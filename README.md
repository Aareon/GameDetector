# GameDetector

Detect games, get Steam info, profit. This program allows you to select a folder on your system (probably in your game library), that contains a game. It looks in the game folder for files that can identify what game it is, get a Steam `AppId` for said game, and find a reasonable list of `EXE` files that could possibly be the game executable.

The purpose of this application is so that it can be used in a much larger application, an automatic game manager for data hoarders. I'll let you know more about this as I develop it, but believe me, it'll be super rad.

## What this program can and won't do

- Does not circumvent DRM
- Does not encourage the use of pirated software
- Uses publicly available APIs provided by Steam or others
- Makes a reasonable attempt to respect ratelimits
- Caches responses to save bandwidth and API load
- Does not download or upload game data to any server

## Compatibility

Currently, only Windows 10/11 are officially supported, but Debian/Linux support is planned in the future.

## Installing

Install [Python 3.10+](https://python.org/downloads)

Clone the repository and install dependency manager (`pipenv`)
```sh
git clone https://github.com/Aareon/GameDetector
cd GameDetector
py -m pip install pipenv
pipenv install
```

After installing dependencies, and given that their were no errors in doing so, run the application.
```sh
py ./src/game_detect.py
```

## What to expect after running

After executing the program, you will be prompted to select a folder (game folder). The program will then attempt to detect what game it is based on things like folder name, as well as checking known files that are commonly available in many distributions of games. It will tell you the name of the game detected, the version, the Steam AppId (if available on Steam), as well as a game description.

This program isn't super useful to regular users, but it will be extremely handy to have as a library for other applications.

## Reporting bugs

Please see [the Issues page](https://github.com/Aareon/GameDetector/issues). Please include the full output from the program.
