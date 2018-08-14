#!/usr/bin/env python2

import sys
import argparse
import cv2
import json
import os
import random
import time
import uuid
import requests

from autobahn.twisted.websocket import WebSocketClientProtocol, \
    WebSocketClientFactory, connectWS
from twisted.python import log
from twisted.internet import reactor, ssl
from threading import Thread

import face_processing as fp
import imutils
# import tts


parser = argparse.ArgumentParser()
parser.add_argument('--host', type=str, default="imwy.apps.exosite.io",
                    help='Websocket server hostname')
parser.add_argument('--port', type=int, default=443,
                    help='Websocket server port')
parser.add_argument('--endpoint', type=str, default="/webcam",
                    help='Websocket endpoint to upload images (ws:// or wss://)')
parser.add_argument('--key', type=str, default="",
                    help='Cloud Vision API key')
args = parser.parse_args()

# Google Cloud Vision
api_key = args.key
feature_type = "FACE_DETECTION"

# Capture frequency
# cap_freq = 0.5
cap_freq = 1

# Capture from camera at location 0
cap = cv2.VideoCapture(0)

# Customize camera resolution
enable_resize = True
cap_width = 400
cap_height = 300
# cap_width = 320
# cap_height = 240

# allow the camera to warmup
time.sleep(0.1)


palette_hex_table = [
  "#a6cee3",
  "#1f78b4",
  "#b2df8a",
  "#33a02c",
  "#fb9a99",
  "#e31a1c",
  "#fdbf6f",
  "#ff7f00",
  "#cab2d6",
  "#6a3d9a"
]
# Convert to BGR colors
palette_bgr_table = [tuple(int(h.lstrip('#')[i:i+2], 16) for i in (4, 2 ,0)) \
    for h in palette_hex_table]

# Pick a color for a new face
def pick_face_color(color_index):
    color_hex = palette_hex_table[color_index % 10]
    color = palette_bgr_table[color_index % 10]
    return color, color_hex


class WebcamClientProtocol(WebSocketClientProtocol):

    def onConnect(self, response):
        print(response)
        print("Server connected: {0}".format(response.peer))

    def onOpen(self):
        self.pingsReceived = 0
        self.pongsSent = 0
        self.prevFrame = None
        print("WebSocket connection opened.")

        # Start to upload images
        print("Start uploading Webcam snapshots ...")
        self.upload_image()

    def onMessage(self, payload, isBinary):
        if isBinary:
            print("Binary message received: {0} bytes".format(len(payload)))
        else:
            print("Text message received: {0}".format(payload.decode('utf8')))

    def onClose(self, wasClean, code, reason):
        print("WebSocket connection closed: {} (code: {})".format(reason, code))

    def onPing(self, payload):
        self.pingsReceived += 1
        print("Ping received from {} - {}".format(self.peer, self.pingsReceived))
        self.sendPong(payload)
        self.pongsSent += 1
        print("Pong sent to {} - {}".format(self.peer, self.pongsSent))

    def upload_image(self):

        # Capture new frame
        ret, frame = cap.read()
        # gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        width = cap.get(cv2.cv.CV_CAP_PROP_FRAME_WIDTH)
        height = cap.get(cv2.cv.CV_CAP_PROP_FRAME_HEIGHT)
        print("Height: ", height)
        print("Width: ", width)

        if enable_resize:
            resized_frame = fp.resize_rgbframe(frame, cap_width, cap_height)
            print("Height: ", resized_frame.shape[0])
            print("Width: ", resized_frame.shape[1])
            frame = resized_frame

        # Detect face from Cloud Vision
        frame = self.detect_face(frame)
        frame, isMotionDetect = self.detect_motion(frame)

        timestamp = time.time()
        data_url = fp.rgbframe_to_data_url(frame)
        msg = {
            'capture_time': timestamp,
            'data_url': data_url,
            'detected_motion': isMotionDetect
        }
        json_string = json.dumps(msg)
        self.sendMessage(json_string)

        # send every cap_freq second
        self.factory.reactor.callLater(cap_freq, self.upload_image)

    def detect_motion(self, frame):
        detectAreas = 0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.prevFrame is None:
            self.prevFrame = gray
        else:
            frameDelta = cv2.absdiff(self.prevFrame, gray)
            self.prevFrame = gray
            thresh = cv2.threshold(frameDelta, 25, 255, cv2.THRESH_BINARY)[1]

            thresh = cv2.dilate(thresh, None, iterations=2)
            cnts = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cnts = cnts[0] if imutils.is_cv2() else cnts[1]
            maxCnt = None
            maxAreaSize = 500 # if the contour is too small, ignore it
            for c in cnts:
                # Only choose the biggest area
                if cv2.contourArea(c) > maxAreaSize:
                    maxCnt = c
                    detectAreas += 1


            if maxCnt is not None:
                # compute the bounding box for the contour, draw it on the frame,
                # and update the text
                (x, y, w, h) = cv2.boundingRect(maxCnt)
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)



            # cv2.imshow("Security Feed", frame)
            # cv2.imshow("Thresh", thresh)
            # cv2.imshow("Frame Delta", frameDelta)
            # key = cv2.waitKey(1) & 0xFF

        if detectAreas > 0:
            print("detect motion!!!", detectAreas)

        return frame, detectAreas > 0

    def detect_face(self, frame):
        content = fp.rgbframe_to_base64(frame)
        request_body = {
          "requests":[
            {
              "image":{
                "content": content
              },
              "features":[
                {
                  "type": feature_type,
                  "maxResults":10
                }
              ]
            }
          ]
        }

        params = {
            "key": api_key
        }
        headers = {
            "Content-Type": "application/json"
        }

        r = requests.post("https://vision.googleapis.com/v1/images:annotate",
            json=request_body, params=params, headers=headers)
        json_data = json.loads(r.text)

        if "responses" in json_data and len(json_data["responses"]) > 0:
            json_data = json_data["responses"][0]
            if "faceAnnotations" in json_data and len(json_data["faceAnnotations"]):
                for face in json_data["faceAnnotations"]:
                    locations = face["boundingPoly"]["vertices"]
                    print(locations)

                    max_x, max_y, min_x, min_y = 0, 0, 0, 0
                    for loc in locations:
                        if "x" in loc:
                            if max_x == 0:
                                max_x = loc["x"]
                            elif max_x < loc["x"]:
                                max_x = loc["x"]
                            if min_x == 0:
                                min_x = loc["x"]
                            elif min_x > loc["x"]:
                                min_x = loc["x"]
                        if "y" in loc:
                            if max_y == 0:
                                max_y = loc["y"]
                            elif max_y < loc["y"]:
                                max_y = loc["y"]
                            if min_y == 0:
                                min_y = loc["y"]
                            elif min_y > loc["y"]:
                                min_y = loc["y"]

                    # Draw a box around the face (color order: BGR)
                    top, right, bottom, left = min_y, max_x, max_y, min_x
                    color, _ = pick_face_color(1)
                    fp.draw_face_box(frame, color, top, right, bottom, left)

        return frame

def main(argv):
    log.startLogging(sys.stdout)

    ws_endpoint = "wss://{}{}".format(args.host, args.endpoint)
    factory = WebSocketClientFactory(ws_endpoint)
    factory.protocol = WebcamClientProtocol

    # SSL client context: default
    if factory.isSecure:
        contextFactory = ssl.ClientContextFactory()
    else:
        contextFactory = None

    connectWS(factory, contextFactory)
    reactor.run()

    # When everything done, release the capture
    cap.release()

if __name__ == '__main__':
    main(sys.argv)
