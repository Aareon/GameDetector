from pathlib import Path
from setuptools import find_packages, setup

setup(
    name='GameDetector',
    packages=find_packages(include=['gamedetector']),
    version='0.2.2',
    description='The GameDetector library allows you to detect a game within a folder, returning information like Steam'
            ' AppId, game name, and version.',
    long_description=(Path("__file__").parent / "README.md").read_text(),
    author='Aareon Sullivan',
    author_email="askully13@gmail.com",
    url="https://github.com/Aareon/GameDetector",
    license='MIT',
)
