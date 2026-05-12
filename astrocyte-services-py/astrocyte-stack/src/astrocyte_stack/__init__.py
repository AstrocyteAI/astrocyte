"""astrocyte-stack — convenience meta-package.

This module exists only so the wheel has something to install. The
real value of ``pip install astrocyte-stack`` is the dependency
declaration in ``pyproject.toml`` that pulls
``astrocyte[default]`` (= ``astrocyte`` + ``astrocyte-postgres``).

For library use, ``import astrocyte`` directly — there is no
``astrocyte_stack`` API to call.
"""

__version__ = "0.1.0"
