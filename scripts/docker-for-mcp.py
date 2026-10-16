#!/usr/bin/env python3
import os
import sys

docker = os.environ.get("PW_DOCKER", "/usr/local/bin/docker")
args = sys.argv[1:]
if args and args[0] == "exec":
    height = int(os.environ.get("PW_VIDEO_MAX_HEIGHT", "1920"))
    args = ["exec", "--env", f"VIDEO_MAX_HEIGHT={height}", *args[1:]]
os.execv(docker, [docker, *args])
