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
from configparser import ConfigParser, NoSectionError
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog
from typing import List
from enum import Enum

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

    def _get_version_number(app_path: Path) -> str | None:
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

    def _get_version_number(app_path: Path) -> None:
        pass


# Useful files to look for
# steam_emu.ini (INI)
# MicrosoftGame.Config (XML)
# app.info (INFO)  # publisher/game name
# steam_api.ini (INI) appid
# ChromaAppInfo.xml (XML) used by Factorio, contains name and description
# TODO: goggame-#.INFO - https://api.gog.com/products/1458058109
#   glob goggame-*.info


class SteamApiException(Exception):
    """Exception class for errors when making Steam API calls"""


class GogApiException(Exception):
    """Exception class for errors when making GOG API calls"""


class NoGameException(Exception):
    """Exception for when a folder probably does not contain a game"""


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


@dataclass
class GogGame:
    gogid: int
    name: str
    publisher: str
    version: str
    description: str
    path: Path


class Game(Enum):
    NoGame = 0
    NonSteamGame = NonSteamGame
    SteamGame = SteamGame
    GogGame = GogGame


# Steam API calls allow up to 200 requests in 5 minutes
@ratelimit.limits(calls=175, period=300, raise_on_limit=True)
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


# GOG API calls allow up to 50 requests in 5 minutes
@ratelimit.limits(calls=50, period=300, raise_on_limit=True)
def gog_api_call(url: str) -> requests.Response | None:
    """Make a call to GOG API with respect to ratelimits.
    Automatically handle a failure and raise GogApiException"""
    resp = None

    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
    except Exception as e:
        raise GogApiException(e)

    if resp.status_code != 200:
        raise GogApiException(
            f"Failed to connect to '{url}', please check your connection and try again later."
            f" Status code: {resp.status_code}"
        )

    return resp


def get_app_description(appid: int = None, gogid: int = None) -> str | None:
    """Get description for game using Steam AppId
    Args:
        appid: int
    Returns:
        description: str
    """
    desc = None

    if appid is not None:
        url = f"http://store.steampowered.com/api/appdetails?appids={appid}"
        try:
            resp = steam_api_call(url)
            data = resp.json()[f"{appid}"]
            if not data.get("success", False):
                logging.error(
                    f"Getting details `success` failed for getting description ({appid})"
                )
            desc = data["data"].get("short_description")
        except SteamApiException as e:
            logging.error(
                f"An error occurred getting `app_description`. Error: {str(e)}",
                exc_info=True,
            )
            return
    elif gogid is not None:
        url = f"https://api.gog.com/products/{gogid}?expand=description"
        resp = gog_api_call(url)
        data = resp.json()
        if data.get("id") != gogid:
            logging.error(
                f"An issue occurred getting description for GOG game `{gogid}`"
            )
        desc = data.get("description").get("lead")

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


def gog_game_from_gogid(gogid: int) -> GogGame:
    name = None
    desc = None

    url = f"https://api.gog.com/products/{gogid}?expand=description"
    resp = gog_api_call(url)
    data = resp.json()

    if data.get("id") != gogid:
        logging.error(f"An issue occurred getting description for GOG game `{gogid}`")

    name = data.get("title")
    desc = data.get("description").get("lead")
    game = GogGame(
        gogid=gogid,
        name=name,
        publisher=None,
        version=None,
        description=desc,
        path=None,
    )
    return game


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
    data = None
    # no use using ConfigParser because every ini is different
    # we'll just manually find `AppId` in the file
    if game_folder.is_file():
        with open(game_folder) as f:
            data = f.read()

    elif game_folder.is_dir():
        for fp in game_folder.glob("**/*.ini"):
            if fp.name.endswith("steam_api.ini"):
                with open(fp) as f:
                    data = f.read()
    else:
        logging.debug("Could not find `steam_api.ini`.")
        return

    if data is None:
        return

    for ln in data.splitlines():
        if ln.startswith("AppId="):
            try:
                appid = ln.replace("AppId=", "").rstrip()
            except ValueError:
                logging.error(f"Could not get AppId from line: '{ln}'")
                return

    # remove leading zeroes
    n = 0
    if appid is not None and appid.startswith("0"):
        for c in appid:
            if c == "0":
                n += 1
        appid = appid[n - 1 :]

    return int(appid)


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


def check_chroma_app_info_xml(game_folder: Path) -> str | None:
    """Check ChromaAppInfo.xml for game name
    Args:
        game_folder: <Path> may be file or folder containing `ChromaAppInfo.xml`"""
    name = None  # detected game name
    fp = None  # path to `ChromaAppInfo.xml`

    if game_folder.is_file() and game_folder.name == "ChromaAppInfo.xml":
        logging.debug("Found ChromaAppInfo.xml")
        fp = game_folder
    elif game_folder.is_dir():
        # fs = [f for f in game_folder.glob('**/*') if f.is_file()]
        for fp in game_folder.glob("**/*.xml"):
            if fp.name.startswith("ChromaAppInfo"):
                logging.debug("Found ChromaAppInfo.xml")
                break
    else:
        logging.debug("Could not find `ChromaAppInfo.xml`.")
        return

    if fp is not None:
        with open(fp, "r") as f:
            for line in f.readlines():
                line = line.lstrip().rstrip()
                query = "<title>"
                i = line.find(query)
                if i != -1:
                    name = line[i + len(query) :].replace("</title>", "")
                    break

    return name


def check_gog_game_info(game_folder: Path) -> int | None:
    gogid = None
    for fp in game_folder.glob("**/goggame-*.info"):
        if fp.name.startswith("goggame-"):
            gogid = int(fp.name.split("-")[1].strip(".info"))
        else:
            logging.debug("Could not find `goggame-*.info`.")
    return gogid


def get_name_from_gogid(gogid: int) -> str | None:
    url = f"https://api.gog.com/products/{gogid}"
    title = None
    try:
        resp = gog_api_call(url)
    except GogApiException as e:
        logging.error(
            f"An error occurred getting `title`. Error: {str(e)}",
            exc_info=True,
        )
        raise

    data = resp.json()
    if data.get("id") != gogid:
        logging.error(f"An issue occurred getting name for GOG game ({gogid})")
        return
    title = data.get("title")
    return title


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
    appid = None
    gogid = None

    logging.info("(7z) Detecting game. Large games may take a little while...")

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
            # special files to check for
            if Path(f).stem.lower().startswith("goggame-"):
                try:
                    gogid = int(Path(f).stem.lower().replace("goggame-", ""))
                    logging.debug(f"Found goggame file, id ({gogid})")
                except ValueError:
                    logging.error(f"Error converting gogid {gogid} to int", exc_info=True)
            # plain filenames to check for
            match Path(f).name.lower():
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
                case "chromaappinfo.xml":
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
                    logging.debug(f"{exe} matched: {' '.join(d.matched for d in m)}")

        logging.debug(f"Remove fuzzed EXEs: {fuzzed_exes}")

        unpack_exes = [f for f in exes if f not in fuzzed_exes]

        if len(unpack_exes) == 0:
            raise NoGameException(f"no binaries detected for {game_path}")

        logging.debug(f"Files to unpack: {helpers + unpack_exes}")

        logging.debug("Unpacking targets...")
        for fname, bio in z.read(helpers + unpack_exes).items():
            # create a temporary directory to store helper files for reading
            tmp = Path(__file__).parent / ".tmp"
            tmp.mkdir(exist_ok=True)
            with open(tmp / Path(fname).name, "wb") as f:
                f.write(bio.read())

    game = detect_folder(Path(__file__).parent / ".tmp")
    game.path = game_path

    logging.debug(f"Game detected in folder (not done): {game}")

    # cleanup tmp again
    if tmp.is_dir():
        shutil.rmtree(tmp)
        logging.debug("Cleaned up `.tmp`")

    if game.name == " tmp" and game.version is None:
        version = get_game_version(game.path)
        if version is None:
            # try to get game from folder name
            name = get_game_name(game_path, "").replace("7z", "").rstrip()
            appid = int(get_appid_from_name(name))
            name = get_name_from_appid(appid)
            game = SteamGame(
                name=get_name_from_appid(appid),
                version=version or "Unknown",
                description=get_app_description(appid),
                publisher=None,
                path=game_path,
                appid=appid,
            )
            if appid is None:
                raise NoGameException(game)
        if version is not None:
            game.version = version.strip(".7z")  # v03.03.2020.7z
            game.name = (
                game.path.name.strip(".7z")
                .replace(version, "")
                .replace(".", " ")
                .rstrip()
            )
    elif game.version is not None and isinstance(game, NonSteamGame):
        game.name = get_game_name(game.path, game.version)

    if game.name is not None and isinstance(game, NonSteamGame):
        # recheck for appid
        appid = get_appid_from_name(game.name)

    if appid is not None and isinstance(game, NonSteamGame):
        # make sure everything is correct, game.name may sometimes include "7z". This fixes that
        # by re checking appid corresponds with game.name
        game = SteamGame(
            name=get_name_from_appid(appid),
            publisher=game.publisher,
            version=game.version,
            description=get_app_description(appid=appid),
            appid=appid,
            path=game.path,
        )
    elif gogid is not None and isinstance(game, NonSteamGame):
        game = GogGame(
            gogid=gogid,
            name=get_name_from_gogid(gogid),
            publisher=game.publisher,
            version=game.version,
            description=get_app_description(gogid=gogid),
            path=game.path
        )

    if isinstance(game, SteamGame):
        game.name = get_name_from_appid(game.appid)

    logging.info(f"(7z) Detected game: {game}")
    return game


def detect_folder(game_folder: Path, gogid=None) -> SteamGame | NonSteamGame:
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
    game_gogid = None

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
        logging.debug(f"steam_api.ini check> appid ({game_appid})")
    if game_appid is None:
        # Check for `app.info`
        game_publisher, game_name = check_app_info(game_folder)
    if game_appid is None:
        # Check for `goggame-*.info`
        game_gogid = check_gog_game_info(game_folder)
        logging.debug(f"Found gogid ({game_gogid})")
        if game_gogid is not None:
            game_name = get_name_from_gogid(int(game_gogid))

    if game_name is None and game_gogid is None:
        logging.debug("Checking for ChromaAppInfo.xml")
        # Check for `ChromaAppInfo.xml`
        game_name = check_chroma_app_info_xml(game_folder)

    if game_name is not None and game_gogid is None:
        game_appid = get_appid_from_name(game_name, app_list)

    if game_version is None:
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

    logging.debug(f"appid={game_appid}, gogid={game_gogid}")

    if game_version is not None:
        game_name = get_game_name(game_folder, game_version, delimiter)
        logging.info(f"Detected game: {game_name}")

    # Try to find game in Steam app_list
    if game_gogid is None:
        for game in app_list["applist"]["apps"]:
            if game["name"] == game_name:
                game_appid = game["appid"]
                logging.debug(f"Detected appid: {game_appid}")

    if game_appid is not None:
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
        logging.debug(f"Single EXE found, checking version for {possible_exes[0]}")
        game_version = _get_version_number(possible_exes[0])
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
        logging.debug(f"Game version: {game_version}, Name: {game_name}")
        # fuzzy match best choice
        if game_version is None and game_name is not None:
            logging.debug(f"Checking version for '{game_name}'")
            if len(no_launchers) != 0:
                for exe in no_launchers:
                    try:
                        m = fuzz.find_near_matches(game_name, exe.name, max_l_dist=1)
                    except ValueError:
                        logging.error(
                            f"Error matching in `no_launchers`: game_name='{game_name}', exe.name='{exe.name}'",
                            exc_info=True)
                        raise
                    if m:
                        logging.debug(f"Fuzzy matched EXE: `{exe}`, match: {m}")
                        logging.debug(
                            "Attempting to get version number using win32 api"
                        )
                        game_version = _get_version_number(exe)

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
            name=get_name_from_appid(game_appid),
            publisher=game_publisher,
            version=game_version,
            path=game_folder,
            description=get_app_description(appid=game_appid) or "N/A",
            appid=game_appid,
        )
    elif game_gogid is not None:
        game = GogGame(
            gogid=game_gogid,
            name=game_name or get_name_from_gogid(game_gogid),
            publisher=None,
            version=game_version or "Unknown",
            description=get_app_description(gogid=game_gogid),
            path=game_folder,
        )
    else:
        logging.debug(f"Last chance before recheck `game_name`: {game_name}")
        game = NonSteamGame(
            name=game_name,
            publisher=game_publisher,
            version=game_version,
            description=game_desc or "",
            path=game_folder,
        )

    # --- RECHECK ---
    logging.debug("Performing recheck...")

    if game.name is not None and game.name.endswith(" 7z"):
        # try to get appid from Steam after removing
        # trailing ` 7z` left over after splitting delimiters
        # no known method for doing this for GOG yet
        game_name = game_name.replace(" 7z", "")
        game_appid = get_appid_from_name(game_name)

        if game_appid is not None:
            game = SteamGame(
                appid=game_appid,
                name=get_name_from_appid(game_appid),
                version=game.version,
                publisher=game.publisher,
                description=get_app_description(appid=game_appid),
                path=game_folder
            )

    return game


def main() -> str:
    # Open a folder select dialog and return Steam appid if game is detected.
    prompt = "7zip archive (1), or folder (2)> "
    choice = input(prompt)
    while choice not in (
        "1",
        "2",
    ):
        choice = input(prompt)

    if choice == "1":
        game_path = Path(filedialog.askopenfilename(filetypes=[("7-zip Files", ".7z")]))
        if game_path == Path("."):
            print("Nothing selected. Quitting...")
            sys.exit(0)
    elif choice == "2":
        game_path = Path(filedialog.askdirectory())
        if game_path == Path("."):
            print("Nothing selected. Quitting...")
            sys.exit(0)

    logging.info(f"Path selected: {game_path.name}")
    if game_path.suffix == ".7z":
        game = detect_7z(game_path)
    elif game_path.is_dir():
        game = detect_folder(game_path)
    else:
        raise Exception(f"Unexpected path: `{game_path}`")

    print(game)


if __name__ == "__main__":
    main()
