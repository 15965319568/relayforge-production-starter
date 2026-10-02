#!/usr/bin/env sh
set -eu
python -m compileall -q src tests
python -m unittest discover -s tests -v

