#!/usr/bin/env python3

import configparser
import getpass
import logging
import os
import re
from pathlib import Path
from typing import NamedTuple, Optional

import requests

import ghstack.logs

DEFAULT_GHSTACKRC_PATH = Path.home() / ".ghstackrc"
GHSTACKRC_PATH_VAR = "GHSTACKRC_PATH"

Config = NamedTuple(
    "Config",
    [
        # Proxy to use when making connections to GitHub
        ("proxy", Optional[str]),
        # OAuth token to authenticate to GitHub with
        ("github_oauth", Optional[str]),
        # GitHub username; used to namespace branches we create
        ("github_username", str),
        # Token to authenticate to CircleCI with
        ("circle_token", Optional[str]),
        # These config parameters are not used by ghstack, but other
        # tools that reuse this module
        # Path to working fbsource checkout
        ("fbsource_path", str),
        # Path to working git checkout (ghstack infers your git checkout
        # based on CWD)
        ("github_path", str),
        # Path to project directory inside fbsource, to default when
        # autodetection fails
        ("default_project_dir", str),
        # GitHub url. Defaults to github.com which is true for all non-enterprise github repos
        ("github_url", str),
        # Name of the upstream remote
        ("remote_name", str),
    ],
)


def get_path_from_env_var(var_name: str) -> Optional[Path]:
    if (path := os.environ.get(var_name)) is not None:
        return Path(path).expanduser().resolve()
    return None


def read_config(
    *,
    request_circle_token: bool = False,
    request_github_token: bool = True,
) -> Config:  # noqa: C901
    config = configparser.ConfigParser()

    config_path = None
    current_dir = Path(os.getcwd())

    while current_dir != current_dir.parent:
        tentative_config_path = "/".join([str(current_dir), ".ghstackrc"])
        if os.path.exists(tentative_config_path):
            config_path = tentative_config_path
            break
        current_dir = current_dir.parent

    write_back = False
    if config_path is None:
        config_path = str(
            get_path_from_env_var(GHSTACKRC_PATH_VAR) or DEFAULT_GHSTACKRC_PATH
        )
        write_back = True

    logging.debug(f"config_path = {config_path}")
    config.read([".ghstackrc", config_path])

    if not config.has_section("ghstack"):
        config.add_section("ghstack")
        write_back = True

    if config.has_option("ghstack", "github_url"):
        github_url = config.get("ghstack", "github_url")
    else:
        github_url = input("GitHub enterprise domain (leave blank for OSS GitHub): ")
        if not github_url:
            github_url = "github.com"
        if not re.match(r"[\w\.-]+\.\w+$", github_url):
            raise RuntimeError(
                f"{github_url} is not a valid domain name (do not include http:// scheme)"
            )
        config.set("ghstack", "github_url", github_url)
        write_back = True

    # Environment variable overrides config file
    # This envvar is legacy from ghexport days
    github_oauth = os.getenv("OAUTH_TOKEN")
    if github_oauth is not None:
        logging.warning(
            "Deprecated OAUTH_TOKEN environment variable used to populate github_oauth--"
            "this is probably not what you intended; unset OAUTH_TOKEN from your "
            "environment to use the setting in .ghstackrc instead."
        )
    if github_oauth is None and config.has_option("ghstack", "github_oauth"):
        github_oauth = config.get("ghstack", "github_oauth")
    if github_oauth is None and request_github_token:
        print("Generating GitHub access token...")
        CLIENT_ID = "89cc88ca50efbe86907a"
        res = requests.post(
            f"https://{github_url}/login/device/code",
            headers={"Accept": "application/json"},
            data={"client_id": CLIENT_ID, "scope": "repo"},
        )
        data = res.json()
        print(f"User verification code: {data['user_code']}")
        print("Go to https://github.com/login/device and enter the code.")
        print("Once you've authorized ghstack, press any key to continue...")
        input()

        res = requests.post(
            f"https://{github_url}/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": CLIENT_ID,
                "device_code": data["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        github_oauth = res.json()["access_token"]
        config.set("ghstack", "github_oauth", github_oauth)
        write_back = True
    if github_oauth is not None:
        ghstack.logs.formatter.redact(github_oauth, "<GITHUB_OAUTH>")

    circle_token = None
    if circle_token is None and config.has_option("ghstack", "circle_token"):
        circle_token = config.get("ghstack", "circle_token")
    if circle_token is None and request_circle_token:
        circle_token = getpass.getpass(
            "CircleCI Personal API token (make one at "
            "https://circleci.com/account/api ): "
        ).strip()
        config.set("ghstack", "circle_token", circle_token)
        write_back = True
    if circle_token is not None:
        ghstack.logs.formatter.redact(circle_token, "<CIRCLE_TOKEN>")

    github_username = None
    if config.has_option("ghstack", "github_username"):
        github_username = config.get("ghstack", "github_username")
    if github_username is None and github_oauth is not None:
        request_url: str
        if github_url == "github.com":
            request_url = f"https://api.{github_url}/user"
        else:
            request_url = f"https://{github_url}/api/v3/user"
        res = requests.get(
            request_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {github_oauth}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        res.raise_for_status()
        github_username = res.json()["login"]
        config.set("ghstack", "github_username", github_username)
        write_back = True
    if github_username is None:
        github_username = input("GitHub username: ")
        if not re.match(
            r"^[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}$", github_username, re.I
        ):
            raise RuntimeError(
                "{} is not a valid GitHub username".format(github_username)
            )
        config.set("ghstack", "github_username", github_username)
        write_back = True

    proxy = None
    if config.has_option("ghstack", "proxy"):
        proxy = config.get("ghstack", "proxy")

    if config.has_option("ghstack", "fbsource_path"):
        fbsource_path = config.get("ghstack", "fbsource_path")
    else:
        fbsource_path = os.path.expanduser("~/local/fbsource")

    if config.has_option("ghstack", "github_path"):
        github_path = config.get("ghstack", "github_path")
    else:
        github_path = os.path.expanduser("~/local/ghstack-pytorch")

    if config.has_option("ghstack", "default_project"):
        default_project_dir = config.get("ghstack", "default_project_dir")
    else:
        default_project_dir = "fbcode/caffe2"

    if config.has_option("ghstack", "remote_name"):
        remote_name = config.get("ghstack", "remote_name")
    else:
        remote_name = "origin"

    if write_back:
        with open(config_path, "w") as f:
            config.write(f)
        logging.info("NB: configuration saved to {}".format(config_path))

    conf = Config(
        github_oauth=github_oauth,
        circle_token=circle_token,
        github_username=github_username,
        proxy=proxy,
        fbsource_path=fbsource_path,
        github_path=github_path,
        default_project_dir=default_project_dir,
        github_url=github_url,
        remote_name=remote_name,
    )
    logging.debug(f"conf = {conf}")
    return conf
