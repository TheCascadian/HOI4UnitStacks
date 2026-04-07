import os
from setuptools import setup
from Cython.Build import cythonize
import numpy

# Dense Color Palette for the setup process
G = "\033[32m"  # Green
Y = "\033[33m"  # Yellow
N = "\033[0m"  # Reset

# Logic: We no longer manually delete .c or build/ folders.
# Cython will check the timestamp of .pyx/.py vs .c/.pyd.
# If the source hasn't changed, it skips compilation.


def main():
    setup(
        ext_modules=cythonize(
            [
                "unitstacks_pipeline.pyx",
            ],
            # force=False ensures we only compile changed files
            force=False,
            # Use multiple CPU cores for faster delta-compilation
            nthreads=4,
            compiler_directives={
                "language_level": "3",
                "boundscheck": False,
                "wraparound": False,
            },
        ),
        include_dirs=[numpy.get_include()],
    )

    print(
        f"{G}[BUILD]{N} Delta-compilation complete. Only changed modules were updated."
    )


if __name__ == "__main__":
    main()
