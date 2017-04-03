#!/usr/bin/env python2
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import txaio
txaio.use_twisted()

from autobahn.twisted.websocket import WebSocketServerProtocol, \
    WebSocketServerFactory
from twisted.python import log
from twisted.internet import reactor

import argparse
import cv2
import json
from PIL import Image
import numpy as np
import os
import StringIO
import urllib
import base64
import time
import uuid
from threading import Thread

import pickle
import os.path
import face_recognition
import tts

thumbnail_size = 48

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=9000,
                    help='WebSocket Port')
parser.add_argument('--model', type=str, default="model/learned_faces.pkl",
                    help='Model file path for learned faces')
args = parser.parse_args()

if not os.path.exists("model"):
    os.makedirs("model")
model_path = args.model

# Unique face object
class Face:

    def __init__(self, uuid, name, embeddings):
        self.uuid = uuid
        self.name = name
        self.embeddings = embeddings

    def __hash__(self):
        return hash(self.uuid)

    def __eq__(self, other):
        return self.uuid == other.uuid

    def __ne__(self, other):
        # Not strictly necessary, but to avoid having both x==y and x!=y
        # True at the same time
        return not(self == other)

    def setName(self, name):
        self.name = name

    def __repr__(self):
        return "{{uuid: {}, name: {}, embeddings[0:5]: {}}}".format(
            self.uuid,
            self.name,
            self.embeddings[0:5])

# Unique face object for drawing
class VizFace(Face):

    def __init__(self, uuid, name, embeddings, color, color_hex):
        Face.__init__(self, uuid, name, embeddings)
        self.color = color
        self.color_hex = color_hex

    def __repr__(self):
        return "{{uuid: {}, name: {}, color: {}, embeddings[0:5]: {}}}".format(
            self.uuid,
            self.name,
            '#' + self.color_hex,
            self.embeddings[0:5])

class FaceLearnerProtocol(WebSocketServerProtocol):

    def __init__(self):
        # Call init function of WebSocketServerProtocol
        super(self.__class__, self).__init__()
        self.images = {}
        self.palette = []
        self.palette_hex = []
        # Load learned model if found
        if os.path.isfile(model_path):
            self.load_model()
        else:
            self.learned_faces = set()
        # A cache set of detected faces for drawing
        self.detected_vizfaces = set()
        # A lookup table for drawn faces
        self.face_table = {}

    def onConnect(self, request):
        print("Client connecting: {0}".format(request.peer))

    def onOpen(self):
        print("WebSocket connection open.")

    def onMessage(self, payload, isBinary):
        raw = payload.decode('utf8')
        msg = json.loads(raw)
        print("Received {} message of length {}.".format(
            msg['type'], len(raw)))
        if msg['type'] == "FRAME":
            start_time = time.time()
            content, faces = self.processFrame(msg['dataURL'])
            msg = {
                "type": "ANNOTATED",
                "content": content,
                "frame_faces": faces,
                "processing_time": "{:.2f}".format(self.processing_time(start_time))
            }
            self.sendMessage(json.dumps(msg))
            # Notify frond-end to draw new frame
            self.sendMessage('{"type": "PROCESSED"}')
        elif msg['type'] == "LABELED":
            # Update labeled name of learned face
            vizface = self.face_table[msg['uuid']]
            if vizface is not None:
                vizface.setName(msg['name'])
                learned = Face(vizface.uuid, vizface.name, vizface.embeddings)
                if vizface in self.detected_vizfaces:
                    self.detected_vizfaces.remove(vizface)
                self.detected_vizfaces.add(vizface)
                if learned in self.learned_faces:
                    self.learned_faces.remove(learned)
                self.learned_faces.add(learned)
                # Update model file
                self.save_model()
                print('FACE LABELED!!!!')
                print("Learned faces: {}".format(len(self.learned_faces)))

                # Play voice
                self.play_speech(vizface.name)
        elif msg['type'] == "PALETTE":
            start_time = time.time()
            colors = msg['colors']
            colors_hex = msg['colors_hex']
            self.palette = colors
            self.palette_hex = colors_hex
        else:
            print("Warning: Unknown message type: {}".format(msg['type']))

    def onClose(self, wasClean, code, reason):
        print("WebSocket connection closed: {0}".format(reason))

    def processFrame(self, dataURL):
        head = "data:image/jpeg;base64,"
        assert(dataURL.startswith(head))
        imgdata = base64.b64decode(dataURL[len(head):])
        imgF = StringIO.StringIO()
        imgF.write(imgdata)
        imgF.seek(0)
        img = Image.open(imgF)
        img_width, img_height = img.size

        # Flip image horizontally
        buf = cv2.flip(np.asarray(img), flipCode=1)
        # Convert BGR to RGB
        rgbFrame = cv2.cvtColor(buf, cv2.COLOR_BGR2RGB)

        # Make a copy for annotation
        annotatedFrame = np.copy(rgbFrame)

        # Convert BGR to GRAY for faster face detection
        grayFrame = cv2.cvtColor(buf, cv2.COLOR_BGR2GRAY)

        ## Dectect Faces ##

        start_time = time.time()
        # Find all the faces and face enqcodings in the frame of Webcam
        face_locations = face_recognition.face_locations(grayFrame)
        print("Time spent on detecting face: {:.2f} ms".format(
            self.processing_time(start_time)
        ))
        start_time = time.time()
        face_encodings = face_recognition.face_encodings(rgbFrame, face_locations)
        print("Time spent on extracting face embeddings: {:.2f} ms".format(
            self.processing_time(start_time)
        ))

        frame_faces = []
        print("Detected faces: {}".format(len(face_encodings)))
        for(top, right, bottom, left), embeddings in zip(face_locations, face_encodings):
            result = self.face_lookup(embeddings)
            color = result.color
            cropped = self.crop_rgbframe(rgbFrame, top, right, bottom, left, (img_width, img_height))
            if cropped.size > 0:
                resized = self.resize_rgbframe(cropped, thumbnail_size, thumbnail_size)
                data_url = self.rgbframe_to_data_url(resized)
                face = {
                    "uuid": result.uuid,
                    "color": result.color_hex,
                    "name": result.name,
                    "thumbnail": data_url
                }
            else:
                face = {
                    "uuid": result.uuid,
                    "color": result.color_hex,
                    "name": result.name
                }
            frame_faces.append(face)

            # Draw a box around the face (color order: BGR)
            cv2.rectangle(annotatedFrame, (left, top), (right, bottom),
                (color['b'], color['g'], color['r']), thickness=2)

            # Draw a labeled name below the face (color order: BGR)
            font = cv2.FONT_HERSHEY_DUPLEX
            cv2.putText(annotatedFrame, result.name, (left, top - 10), font, fontScale=0.75,
                color=(color['b'], color['g'], color['r']), thickness=2)

        start_time = time.time()
        # Generate image data url from annotated frame
        content = self.rgbframe_to_data_url(annotatedFrame)
        print("Time spent on converting image to data url: {:.2f} ms".format(
            self.processing_time(start_time)
        ))

        return content, frame_faces

    def rgbframe_to_data_url(self, frame):
        png_encoded = cv2.imencode('.png', frame)
        data_url = 'data:image/png;base64,' + \
            urllib.quote(base64.b64encode(png_encoded[1]))
        return(data_url)

    def trim_to_bounds(self, rect, image_shape):
        # Make sure a tuple in (top, right, bottom, left) order is within the bounds of the image
        return max(rect[0], 0), min(rect[1], image_shape[1]), min(rect[2], image_shape[0]), max(rect[3], 0)

    def crop_rgbframe(self, frame, top, right, bottom, left, image_shape):
        top, right, bottom, left = self.trim_to_bounds((top, right, bottom, left), image_shape)
        width = right - left
        height = bottom - top
        cropped = frame[top:(top + height), left:(left + width)]
        return(cropped)

    def resize_rgbframe(self, frame, width, height):
        resized = cv2.resize(frame, (width,height))
        return(resized)

    def processing_time(self, start_time):
        elapsed = (time.time() - start_time) * 1000 # ms
        return(elapsed)

    def face_lookup(self, unknown):
        tolerance = 0.6
        # Lookup from detected faces first
        for known in self.detected_vizfaces:
            matched = self.compare_faces(known.embeddings, unknown, tolerance)
            if matched:
                print("DETECTED!!!!")
                return known

        for known in self.learned_faces:
            matched = self.compare_faces(known.embeddings, unknown, tolerance)
            if matched:
                color, color_hex = self.pick_face_color()
                vizface = VizFace(known.uuid, known.name, known.embeddings, color, color_hex)
                self.detected_vizfaces.add(vizface)
                self.face_table[known.uuid] = vizface
                print("LEANRED!!!!")
                return vizface

        # Not found, create a new one
        uid = str(uuid.uuid4())
        name = "Unknown"
        color, color_hex = self.pick_face_color()
        vizface = VizFace(uid, name, unknown, color, color_hex)
        self.detected_vizfaces.add(vizface)
        self.face_table[uid] = vizface

        return vizface

    # Pick a color for a new face
    def pick_face_color(self):
        color_index = len(self.detected_vizfaces) if len(self.detected_vizfaces) > 0 else 0
        color = self.palette[color_index % 10]
        color_hex = self.palette_hex[color_index % 10]

        return color, color_hex

    def load_model(self):
        with open(model_path, "rb") as f:
            model = pickle.load(f)
            if model is not None:
                print("Model face count: {}".format(len(model)))
                self.learned_faces = model

    def save_model(self):
        with open(model_path, "wb") as f:
            pickle.dump(self.learned_faces, f,
                protocol=pickle.HIGHEST_PROTOCOL)

    def compare_faces(self, known, unknown, tolerance=0.6):
        return self.L2_distance(known, unknown) <= tolerance

    # Compute Euclidean distance
    def L2_distance(self, faces, face_to_compare):
        return np.linalg.norm(faces - face_to_compare)

    def play_speech(self, text):
        thread = Thread(target=self.text_to_speech, args=(text,))
        thread.daemon = True # Daemonize thread
        thread.start()       # Start the execution

    def text_to_speech(self, text):
        start_time = time.time()
        tts.text_to_speech("The face of {} has been labeled.".format(text))
        print("Time spent on text to speech: {:.2f} ms".format(
            self.processing_time(start_time)
        ))
        

if __name__ == '__main__':
    log.startLogging(sys.stdout)

    factory = WebSocketServerFactory("ws://localhost:{}".format(args.port))
    factory.protocol = FaceLearnerProtocol

    reactor.listenTCP(args.port, factory)
    reactor.run()
