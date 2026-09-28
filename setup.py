from setuptools import setup, find_packages

setup(
    name="safe-drive-ejector",
    version="1.1.2",
    description="Native macOS External Disk Safe Ejector & Auto-Remounter (Universal 2: Apple Silicon & Intel)",
    author="Siraj",
    url="https://github.com/siraj-bd/Safe-Drive-Ejector-macOS",
    license="MIT",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "safe-eject=main:main",
        ],
    },
    python_requires=">=3.8",
    install_requires=[],
)
