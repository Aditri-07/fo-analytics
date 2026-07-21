from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup

ext_modules = [
    Pybind11Extension(
        "fo_engine",
        ["cpp/hello.cpp"],
        cxx_std=17,
    ),
]

setup(
    name="fo_engine",
    version="0.1.0",
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
)
