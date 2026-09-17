# How to customise this template

Run `python3 customise.py` to be walked through the steps below and have them applied for you (STRONGLY RECOMMENDED), or work through them by hand:

1. Name your repository with the name `fileformats-vendor-<YOUR-VENDOR-NAME>`
1. Rename the `fileformats/vendor/CHANGEME/MIMELIKE.py` and `extras/fileformats/extras/vendor/CHANGEME/MIMELIKE.py` to the name of the "mime-like" type of the data the formats hold, e.g. `fileformats/vendor/CHANGEME/medimage.py`, `fileformats/vendor/CHANGEME/biosig.py`
1. Rename the `fileformats/vendor/CHANGEME` and `extras/fileformats/vendor/CHANGEME` directories to the name of the vendor, i.e.
1. Search and replace "CHANGEME" with the name of the vendor across all files in the repository
1. Replace name + email placeholders in `pyproject.toml` for developers and maintainers
1. Implement extension file-format classes in the `fileformats/vendor/<YOUR-VENDOR-NAME>/<YOUR-MIMELIKE-TYPE>.py`
1. Implement "extra" functions (see the [extras developer guide](https://arcanaframework.github.io/fileformats/developer/extras.html)) in `extras/fileformats/extras/vendor/<YOUR-VENDOR-NAME>/<YOUR-MIMELIKE-TYPE>.py`
1. Delete these instructions in this README

...

# FileFormats Vendor CHANGEME

[![CI/CD](https://github.com/arcanaframework/fileformats-vendor-CHANGEME/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/arcanaframework/fileformats-vendor-CHANGEME/actions/workflows/ci-cd.yml)
[![Codecov](https://codecov.io/gh/arcanaframework/fileformats-vendor-CHANGEME/branch/main/graph/badge.svg?token=UIS0OGPST7)](https://codecov.io/gh/arcanaframework/fileformats-vendor-CHANGEME)
![Static Badge](https://img.shields.io/badge/type%20checked-mypy-039dfc)
[![Python Versions](https://img.shields.io/pypi/pyversions/fileformats-vendor-CHANGEME.svg)](https://pypi.python.org/pypi/fileformats-vendor-CHANGEME/)
[![Latest Version](https://img.shields.io/pypi/v/fileformats-vendor-CHANGEME.svg)](https://pypi.python.org/pypi/fileformats-vendor-CHANGEME/)
[![Documentation Status](https://img.shields.io/badge/docs-latest-brightgreen.svg?style=flat)](https://arcanaframework.github.io/fileformats-vendor-CHANGEME/)

The extension package of [Fileformats](https://github.com/arcanaframework/fileformats) provides a library of Python classes types for recognising and handling vendor-specific formats for CHANGEME files.


## Quick Installation

This extension can be installed for Python 3 using *pip*::

    $ pip3 install fileformats-vendor-CHANGEME

This will install the core package and any other dependencies


## License

This work is licensed under the [Apache License, Version 2.0](http://www.apache.org/licenses/LICENSE-2.0).
