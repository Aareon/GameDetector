"""detect_games.py

Detect name of a game based on folder name or EXE name.

- Running this script directly will open a folder select dialog.
- Select a folder that contains a game.
- The script will attempt to detect what game is within and return a Steam appid, if possible.
"""
import logging
import re
import shutil
import sys
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog
from typing import List

import fuzzysearch as fuzz
import py7zr
import ratelimit
import requests
from detect_delimiter import detect

try:
    import ujson as json
except ImportError:
    import json

logger = logging.basicConfig(
    filename="gamedetector.log",
    format="%(asctime)s - %(name)s - %(lineno)d -  %(message)s",
    level=logging.DEBUG,
)

try:
    from win32api import HIWORD, LOWORD, GetFileVersionInfo
    from win32api import error as win32error
    from win32com.client import Dispatch

    _using_win32 = True

    def get_version_number(app_path: Path) -> str | None:
        app_path = str(app_path.resolve())
        try:
            info = GetFileVersionInfo(app_path, "\\")
            ms = info["FileVersionMS"]
            ls = info["FileVersionLS"]
            return f"{HIWORD(ms)}.{LOWORD(ms)}.{HIWORD(ls)}.{LOWORD(ls)}"
        except win32error:
            logging.debug(
                "(win32) Exception occurred getting version number", exc_info=True
            )
            parser = Dispatch("Scripting.FileSystemObject")
            version = parser.GetFileVersion(app_path) or None
            return version

except ImportError:
    _using_win32 = False

    def get_version_number(app_path: Path) -> None:
        pass


# Useful files to look for
# steam_emu.ini (INI)
# MicrosoftGame.Config (XML)
# app.info (INFO)  # publisher/game name
# steam_api.ini (INI) appid
# TODO ChromaAppInfo.xml (XML) used by Factorio, contains name and description


class SteamApiException(Exception):
    """Exception class for errors when making Steam API calls"""


@dataclass
class NonSteamGame:
    name: str
    publisher: str
    version: str
    description: str
    path: Path


@dataclass
class SteamGame(NonSteamGame):
    appid: int
    name: str
    publisher: str
    version: str
    description: str
    path: Path


# Steam API calls allow up to 200 requests in 5 minutes
@ratelimit.limits(calls=175, period=300)
def steam_api_call(url: str) -> requests.Response | None:
    """Make a call to Steam API with respect to ratelimits.
    Automatically handle a failure and raise SteamApiException"""
    resp = None

    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
    except Exception as e:
        raise SteamApiException(e)

    if resp.status_code != 200:
        raise SteamApiException(
            f"Failed to connect to '{url}', please check your connection and try again later."
            f" Status code: {resp.status_code}"
        )

    return resp


def get_app_description(appid: int) -> str | None:
    url = f"http://store.steampowered.com/api/appdetails?appids={appid}"
    desc = None
    try:
        resp = steam_api_call(url)
    except SteamApiException as e:
        logging.error(
            f"An error occurred getting `app_description`. Error: {str(e)}",
            exc_info=True,
        )

    data = resp.json()
    if data[f"{appid}"].get("success", False):
        desc = data[f"{appid}"]["data"]["short_description"]
    return desc


def get_app_list(should_update=False) -> dict:
    # get app list from Steam
    app_list_fp = Path(__file__).parent / "app_list.json"
    # mod_time = app_list_fp.stat().st_mtime  # modified time
    # hr_12_ts = 86400000 / 2  # 24 hours in (ms)
    # past_12_hrs = time.time() - mod_time >= hr_12_ts  # 12 hours has passed

    # logging.debug(f"`app_list.json` last updated {datetime.fromtimestamp(mod_time)}")

    if not app_list_fp.exists():
        # if past_12_hrs:
        # logging.debug("12 hours has passed, updating `app_list.json`")
        try:
            resp = steam_api_call(
                "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
            )
        except SteamApiException as e:
            logging.error(
                "Failed to get app_list from Steam API. Please check your connection and try again later."
                f" Error: {str(e)}.",
                exc_info=True,
            )
            sys.exit(1)

        app_list = resp.json()

        # save Steam app_list to save bandwidth
        with open(app_list_fp, "w") as f:
            json.dump(app_list, f)
    else:
        with open(app_list_fp, "r") as f:
            app_list = json.load(f)

    return app_list


def steam_game_from_appid(game_appid: int, app_list: dict = {}) -> SteamGame:
    if app_list == {}:
        # download app_list
        app_list = get_app_list()

    for game in app_list["applist"]["apps"]:
        if game["appid"] == game_appid:
            game_name = game["name"]
            return SteamGame(name=game_name, appid=game_appid)


def get_game_version(
    game_folder: Path, delimiter: str = ".", is_folder=True
) -> str | None:
    """Get game version from version identifier in folder name"""
    game_version = None

    # to disclude game iterations, get software version if available
    if is_folder:
        name_and_soft_ver = game_folder.name.split(delimiter)

        # software version may have been split
        # check if an item in `name_and_soft_ver` is equivalent to `v#+` where # is a number
        # this is done to create a full software version
        for i, e in enumerate(name_and_soft_ver):
            m = re.match("(?i)(v[0-9]+)", e)  # case insensitive
            if m is not None:
                logging.debug(f"Matched version segment: {m}")
                game_version = ".".join(name_and_soft_ver[i:])
                return game_version

        for fp in game_folder.glob("**/version"):
            if fp.name.endswith("version.txt") or fp.name.endswith("version"):
                logging.info(f"Found version file: `{fp}`")
                with open(fp) as f:
                    game_version = f.readline().strip()
                    break
    else:  # check application version in binary name
        logging.debug(f"Checking application version for `{game_folder.name}`")
        comp = game_folder.name.split(" ")
        for c in comp:
            m = re.match("([vV][0-9]+)", c)
            if m:
                game_version = c

    return game_version


def check_steam_api_ini(game_folder: Path) -> int | None:
    appid = None
    steam_api = ConfigParser()
    if game_folder.is_file():
        steam_api.read(game_folder)
        appid = steam_api["Steam"]["AppId"].lstrip().rstrip()
    elif game_folder.is_dir():
        for fp in game_folder.glob("**/*.ini"):
            if fp.name.endswith("steam_api.ini"):
                steam_api.read(fp)
                appid = steam_api["Steam"]["AppId"].lstrip().rstrip()

    # remove leading zeroes
    n = 0
    if appid is not None and appid.startswith("0"):
        for c in appid:
            if c == "0":
                n += 1
        appid = appid[n - 1 :]
    return appid


def check_steam_emu(game_folder: Path) -> int | None:
    """Recursively check game folder for `steam_emu.ini`, which may contain the games AppId.
    This is the preferred method and first choice when detecting the game."""

    game_appid = None

    # Check if `steam_emu.ini` exists in folder. This file contains AppId.
    # Check a list of all files in this directory and subfolders for `steam_emu.ini`
    steam_emu = ConfigParser()
    for fp in game_folder.glob("**/*.ini"):
        if fp.name.endswith("steam_emu.ini"):
            steam_emu.read(fp)
            try:
                game_appid = int(steam_emu["Settings"]["AppId"])
                return game_appid

            except KeyError:
                logging.debug("steam_emu.ini did not contain 'AppId' key.")

            except ValueError:
                logging.debug("steam_emu.ini 'AppId' was not an integer.")

    logging.debug("Could not find `steam_emu.ini`")


def check_appid_txt(game_folder: Path) -> int | None:
    """Check for `steam_appid.txt`
    Returns appid if `steam_appid.txt` exists, None if not
    """
    appid = None
    for fp in game_folder.glob("**/*.txt"):
        if fp.name.endswith("steam_appid.txt"):
            with open(fp) as f:
                appid = int(f.read().strip())
                return appid

    logging.debug("Could not find `steam_appid.txt`.")


def check_app_info(game_folder: Path) -> List[str] | None:
    """Get publisher/game name from app.info in game folder"""
    # TODO improve type hint, return is [publisher str, game name str] (mmm type annotations)
    game_publisher = None
    game_name = None
    for fp in game_folder.glob("**/*.info"):
        if fp.name.endswith("app.info"):
            with open(fp) as f:
                game_publisher, game_name = f.read().splitlines()
                return [game_publisher, game_name]

    logging.debug("Could not find `app.info`.")
    return game_publisher, game_name


def check_launcher_settings_json(game_folder: Path) -> str | None:
    """Get game version from `launcher-settings.json`.
    Created to detect Prison Architect as a last ditch effort."""
    game_version = None
    logging.debug("Checking for `launcher-settings.json`")
    for fp in game_folder.glob("**/*.json"):
        if fp.name.endswith("launcher-settings.json"):
            try:
                with open(fp) as f:
                    game_version = json.load(f)["version"]
                    return game_version
            except UnicodeDecodeError as e:
                logging.debug(
                    f"An error occurred reading `launcher-settings.json`. Error: {str(e)}",
                    exc_info=True,
                )


def get_game_name(
    game_folder: Path, game_version: str = None, delimiter: str = "."
) -> str:
    """Using the path to a game, get the game name."""
    if game_version and game_version in game_folder.name:
        game_name = game_folder.name[
            : game_folder.name.index(game_version) - 1
        ].replace(delimiter, " ")
    else:
        game_name = game_folder.name.replace(delimiter, " ")

    return game_name


def get_game_executables(game_folder: Path) -> List[Path] | List:
    """Get game executables in folder"""
    # TODO fuzzy search known redists (ie. vc_redist) and remove from list (done!)
    with open(Path(__file__).parent / "app_exes.json") as f:
        fj = json.load(f)
        ignore = [a["filename"].lower() for a in fj["exes"]]
        fuzzers = fj["fuzz"]

    exes = []
    for e in game_folder.glob("**/*.exe"):
        for z in fuzzers:
            m = fuzz.find_near_matches(z, e.name.lower(), max_l_dist=1)
            if m:
                logging.debug(f"Fuzzy matched on: `{e.name.lower()}`, fuzzer: `{z}`")
                ignore.append(e.name.lower())
        if e.name.lower() not in ignore:
            exes.append(e)
    return exes


def get_appid_from_name(game_name: str, app_list: dict = {}) -> int | None:
    if app_list == {}:
        # download app_list
        app_list = get_app_list()

    for game in app_list["applist"]["apps"]:
        if game["name"] == game_name:
            game_appid = game["appid"]
            return game_appid


def get_name_from_appid(appid: int, app_list: dict = {}) -> str:
    if app_list == {}:
        # download app_list
        app_list = get_app_list()

    for game in app_list["applist"]["apps"]:
        if game["appid"] == appid:
            game_name = game["name"]
            return game_name


def detect_7z(game_path: Path) -> SteamGame | NonSteamGame:
    """Perform detection on a 7z-compressed game
    Args:
        game_path: Path
    Returns:
        SteamGame | NonSteamGame
    """

    # cleanup .tmp directory
    tmp = Path(__file__).parent / ".tmp"
    if tmp.is_dir():
        shutil.rmtree(tmp)

    if game_path.suffix != ".7z":
        raise AttributeError("Path is not a 7-zip file")

    logging.debug(f"Ext: `{game_path.suffix}`")

    with open(Path(__file__).parent / "app_exes.json") as f:
        j = json.load(f)
        ignore_exes = j["exes"]
        fuzzers = j["fuzz"]

    with py7zr.SevenZipFile(game_path) as z:
        fnames = z.getnames()  # get names of all files in archive
        target_files = []
        helpers = []

        # find helper files
        for f in fnames:
            match Path(f).name:
                case "tipsy.ini":
                    helpers.append(f)
                case "steam_api.ini":
                    helpers.append(f)
                case "steam_emu.ini":
                    helpers.append(f)
                case "app.info":
                    helpers.append(f)
                case "appid.txt":
                    helpers.append(f)
                case _:
                    # not a file we care about
                    pass

        if len(target_files) > 0:
            logging.debug(f"Found helper(s): {target_files}")
        else:
            logging.debug("No helper files found in archive.")

        ignore = [f["filename"].lower() for f in ignore_exes]
        exes = [
            f
            for f in fnames
            if f.endswith(".exe")
            if Path(f).name.lower() not in ignore
        ]

        logging.debug(f"Found EXEs: {exes}")

        fuzzed_exes = set()

        for i, exe in enumerate(exes):
            for fz in fuzzers:
                m = fuzz.find_near_matches(fz, exe, max_l_dist=1)
                if m:
                    fuzzed_exes.add(exe)
                    logging.debug(f"{exe} matched: {m}")

        logging.debug(f"Fuzzed EXEs: {fuzzed_exes}")

        unpack_exes = [f for f in exes if f not in fuzzed_exes]

        logging.debug(f"Files to unpack: {helpers + unpack_exes}")

        logging.debug("Unpacking targets...")
        for fname, bio in z.read(helpers + unpack_exes).items():
            # create a temporary directory to store helper files for reading
            tmp = Path(__file__).parent / ".tmp"
            tmp.mkdir(exist_ok=True)
            with open(tmp / Path(fname).name, "wb") as f:
                f.write(bio.read())

    game = detect_folder(Path(__file__).parent / ".tmp")
    logging.info(f"(7z) Detected game: {game}")

    # cleanup tmp again
    if tmp.is_dir():
        shutil.rmtree(tmp)
        logging.debug("Cleaned up `.tmp`")


def detect_folder(game_folder: Path) -> SteamGame | NonSteamGame:
    """Perform detection on a game folder.

    Args:
        game_folder: Path
    Returns:
        SteamGame | NonSteamGame
    """
    game_name = None
    game_publisher = None
    game_version = None
    game_desc = None
    game_appid = None

    app_list = get_app_list()
    logging.debug("Got app_list, proceeding...")

    # Check for `steam_emu.ini`
    game_appid = check_steam_emu(game_folder)
    if game_appid is None:
        # Check `steam_appid.txt`
        game_appid = check_appid_txt(game_folder)
        if game_appid is None:
            # Check for `steam_api.ini`
            game_appid = check_steam_api_ini(game_folder)
            if game_appid is None:
                # Check for `app.info`
                game_publisher, game_name = check_app_info(game_folder)
                if game_name is not None:
                    game_appid = get_appid_from_name(game_name, app_list)
                    logging.debug(f"Found `app.info` - AppId: {game_appid}")
        else:
            logging.debug(f"Found `steam_appid.txt` - AppId: {game_appid}")
    else:
        logging.debug(f"Found `steam_emu.ini` - AppId: {game_appid}")

    if game_appid is None:
        logging.info(
            "Could not find any helper files. Trying to find game with folder name..."
        )
        # detect if folder name contains delimiters
        # tested with folder names with spaces and periods
        # blacklisted hyphen ("-") as scene groups usually use it to separate their group name from game information
        delimiter = detect(
            game_folder.name, default=".", blacklist="-", whitelist=[".", "_"]
        )
        logging.debug(f"Detected delimiter: '{delimiter}'")

        game_version = get_game_version(game_folder, delimiter)

        game_name = get_game_name(game_folder, game_version, delimiter)
        logging.info(f"Detected game: {game_name}")

        # Find game in app_list
        game_appid = None
        for game in app_list["applist"]["apps"]:
            if game["name"] == game_name:
                game_appid = game["appid"]
                logging.debug(f"Detected appid: {game_appid}")

    if game_appid is not None:
        game_desc = get_app_description(game_appid)
        logging.info(f"Game description: {game_desc}")
        if game_name is None:
            game_name = get_name_from_appid(game_appid, app_list)

    possible_exes = get_game_executables(game_folder)
    if len(possible_exes) > 0:
        logging.debug(possible_exes)
    else:
        logging.warning("\nNo EXE detected. Are you sure this folder contains a game?")

    # More game_version detection. Try with win32 api.
    if len(possible_exes) == 1 and game_version is None:
        # on Windows use win32api to get application version
        game_version = get_version_number(possible_exes[0])
        if game_version is None:
            # Try to find game_version in EXE name
            game_version = get_game_version(possible_exes[0], is_folder=False)
        else:
            logging.debug("Found game_version with win32api")

    elif len(possible_exes) > 1 or game_version is None:
        # attempt to remove launcher binaries and check version on left over EXE (if 1)
        no_launchers = [
            exe for exe in possible_exes if "launcher" not in exe.name.lower()
        ]
        logging.debug(f"EXEs not including launchers: {no_launchers}")
        # fuzzy match best choice
        if game_version is None:
            if len(no_launchers) != 0:
                for exe in no_launchers:
                    m = fuzz.find_near_matches(game_name, exe.name, max_l_dist=1)
                    if m:
                        logging.debug(f"Fuzzy matched EXE: `{exe}`, match: {m}")
                        logging.debug(
                            "Attempting to get version number using win32 api"
                        )
                        game_version = get_version_number(exe)

    if game_version is None:
        # Some games offer `launcher-settings.json` (i.e. Prison Architect)
        game_version = check_launcher_settings_json(game_folder)

    if game_desc is None and game_appid is not None:
        game_desc = get_app_description(game_appid)

    if game_name is None and game_appid is not None:
        game_name = get_name_from_appid(int(game_appid))

    logging.info(f"Detected game version: '{game_version or 'Unknown'}'")
    if game_appid is not None:
        game = SteamGame(
            name=game_name,
            publisher=game_publisher,
            version=game_version,
            path=game_folder,
            description=game_desc or "",
            appid=game_appid,
        )
    else:
        game = NonSteamGame(
            name=game_name,
            publisher=game_publisher,
            version=game_version,
            description=game_desc or "",
            path=game_folder,
        )
    return game


def main() -> str:
    # Open a folder select dialog and return Steam appid if game is detected.
    game_folder = Path(filedialog.askopenfilename(filetypes=[("7-zip Files", ".7z")]))
    if game_folder == Path("."):
        print("Nothing selected. Quitting...")
        sys.exit(0)
    logging.info(f"Game folder selected: {game_folder.name}")
    # detect_folder(game_folder)
    detect_7z(game_folder)


if __name__ == "__main__":
    main()
