$ErrorActionPreference = "Stop"
python -m compileall -q src tests
python -m unittest discover -s tests -v

