"""
Copyright (c) 2023-2024 Bipinkrish
This file is part of the Zlibrary-API by Bipinkrish
Zlibrary-API / Zlibrary.py

For more information, see:
https://github.com/bipinkrish/Zlibrary-API/

Modified by zlib-skill contributors: added dynamic mirrors, timeouts, streaming downloads,
and network-target validation. See THIRD_PARTY_NOTICES.md for the upstream MIT license.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import requests

from .network_safety import safe_get, validate_http_url

DEFAULT_TIMEOUT = (10, 60)
DOWNLOAD_TIMEOUT = (10, 300)
LEGACY_IN_MEMORY_DOWNLOAD_LIMIT = 100 * 1024 * 1024


class Zlibrary:
    def __init__(
        self,
        email: str = None,
        password: str = None,
        remix_userid: [int, str] = None,
        remix_userkey: str = None,
    ):
        self.__email: str
        self.__name: str
        self.__kindle_email: str
        self.__remix_userid: [int, str]
        self.__remix_userkey: str
        self.__domain = "1lib.sk"

        self.__loggedin = False
        self.__session = requests.Session()
        self.__headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
                "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            ),
            "accept-language": "en-US,en;q=0.9",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
            ),
        }
        self.__cookies = {
            "siteLanguageV2": "en",
        }

        if email is not None and password is not None:
            self.login(email, password)
        elif remix_userid is not None and remix_userkey is not None:
            self.__checkIDandKey(remix_userid, remix_userkey)

    def setDomain(self, domain: str):
        """动态切换 Z-Library 镜像站域名"""
        value = domain.strip().lower().rstrip(".")
        parsed = urlparse(f"//{value}")
        if (
            not parsed.hostname
            or parsed.hostname != value
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise ValueError("Z-Library domain must be a hostname without a scheme, path, or port")
        validate_http_url(f"https://{value}", require_https=True, resolve_dns=False)
        self.__domain = value

    def getDomain(self) -> str:
        """获取当前使用的域名"""
        return self.__domain

    def __setValues(self, response) -> dict[str, str]:
        if not response["success"]:
            return response
        self.__email = response["user"]["email"]
        self.__name = response["user"]["name"]
        self.__kindle_email = response["user"]["kindle_email"]
        self.__remix_userid = str(response["user"]["id"])
        self.__remix_userkey = response["user"]["remix_userkey"]
        self.__cookies["remix_userid"] = self.__remix_userid
        self.__cookies["remix_userkey"] = self.__remix_userkey
        self.__loggedin = True
        return response

    def __login(self, email, password) -> dict[str, str]:
        return self.__setValues(
            self.__makePostRequest(
                "/eapi/user/login",
                data={
                    "email": email,
                    "password": password,
                },
                override=True,
            )
        )

    def __checkIDandKey(self, remix_userid, remix_userkey) -> dict[str, str]:
        return self.__setValues(
            self.__makeGetRequest(
                "/eapi/user/profile",
                cookies={
                    "siteLanguageV2": "en",
                    "remix_userid": str(remix_userid),
                    "remix_userkey": remix_userkey,
                },
            )
        )

    def login(self, email: str, password: str) -> dict[str, str]:
        return self.__login(email, password)

    def __makePostRequest(self, url: str, data: dict = None, override=False) -> dict[str, str]:
        if not self.isLoggedIn() and override is False:
            raise RuntimeError("Z-Library client is not logged in")

        if data is None:
            data = {}

        response = self.__session.post(
            "https://" + self.__domain + url,
            data=data,
            cookies=self.__cookies,
            headers=self.__headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=False,
        )
        response.raise_for_status()
        if response.is_redirect:
            raise requests.TooManyRedirects("Z-Library API unexpectedly redirected a POST request")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Z-Library API returned a non-object JSON response")
        return payload

    def __makeGetRequest(self, url: str, params: dict = None, cookies=None) -> dict[str, str]:
        if not self.isLoggedIn() and cookies is None:
            raise RuntimeError("Z-Library client is not logged in")

        if params is None:
            params = {}

        response = self.__session.get(
            "https://" + self.__domain + url,
            params=params,
            cookies=self.__cookies if cookies is None else cookies,
            headers=self.__headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=False,
        )
        response.raise_for_status()
        if response.is_redirect:
            raise requests.TooManyRedirects("Z-Library API unexpectedly redirected a GET request")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Z-Library API returned a non-object JSON response")
        return payload

    def getProfile(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/user/profile")

    def getMostPopular(self, switch_language: str = None) -> dict[str, str]:
        if switch_language is not None:
            return self.__makeGetRequest(
                "/eapi/book/most-popular", {"switch-language": switch_language}
            )
        return self.__makeGetRequest("/eapi/book/most-popular")

    def getBookInfo(
        self, bookid: [int, str], hashid: str, switch_language: str = None
    ) -> dict[str, str]:
        if switch_language is not None:
            return self.__makeGetRequest(
                f"/eapi/book/{bookid}/{hashid}", {"switch-language": switch_language}
            )
        return self.__makeGetRequest(f"/eapi/book/{bookid}/{hashid}")

    def search(
        self,
        message: str = None,
        yearFrom: int = None,
        yearTo: int = None,
        languages: str = None,
        extensions: [str] = None,
        order: str = None,
        page: int = None,
        limit: int = None,
    ) -> dict[str, str]:
        return self.__makePostRequest(
            "/eapi/book/search",
            {
                k: v
                for k, v in {
                    "message": message,
                    "yearFrom": yearFrom,
                    "yearTo": yearTo,
                    "languages": languages,
                    "extensions[]": extensions,
                    "order": order,
                    "page": page,
                    "limit": limit,
                }.items()
                if v is not None
            },
            override=True,
        )

    def __getImageData(self, url: str) -> requests.Response.content:
        with safe_get(
            self.__session,
            url,
            headers=self.__headers,
            timeout=DEFAULT_TIMEOUT,
        ) as res:
            res.raise_for_status()
            return res.content

    def __getBookFileInfo(self, bookid: [int, str], hashid: str) -> tuple[str, str]:
        response = self.__makeGetRequest(f"/eapi/book/{bookid}/{hashid}/file")
        if not response or "file" not in response:
            raise ValueError("Z-Library did not return file metadata")

        filename = response["file"]["description"]

        try:
            filename += " (" + response["file"]["author"] + ")"
        except KeyError:
            pass
        finally:
            filename += "." + response["file"]["extension"]

        ddl = response["file"]["downloadLink"]
        return filename, ddl

    def __getBookFile(
        self,
        bookid: [int, str],
        hashid: str,
        max_bytes: int = LEGACY_IN_MEMORY_DOWNLOAD_LIMIT,
    ) -> tuple[str, bytes]:
        filename, ddl = self.__getBookFileInfo(bookid, hashid)
        with safe_get(
            self.__session,
            ddl,
            headers=self.__headers,
            timeout=DOWNLOAD_TIMEOUT,
        ) as res:
            res.raise_for_status()
            content_length = res.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = None
                if declared_size is not None and declared_size > max_bytes:
                    raise ValueError("Download exceeds the in-memory size limit")
            content = bytearray()
            for chunk in res.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ValueError("Download exceeds the in-memory size limit")
            return filename, bytes(content)

    def getBookDownload(self, bookid: [int, str], hashid: str) -> tuple[str, str]:
        return self.__getBookFileInfo(bookid, hashid)

    def downloadUrlToPath(
        self,
        download_url: str,
        path: [str, Path],
        chunk_size: int = 1024 * 256,
        max_bytes: int | None = None,
    ) -> int:
        """Stream a previously resolved Z-Library download URL to a local path."""

        path = Path(path)
        bytes_written = 0
        with safe_get(
            self.__session,
            download_url,
            headers=self.__headers,
            timeout=DOWNLOAD_TIMEOUT,
            stream=True,
        ) as res:
            res.raise_for_status()
            content_length = res.headers.get("content-length")
            if max_bytes is not None and content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = None
                if declared_size is not None and declared_size > max_bytes:
                    raise ValueError("Download exceeds the configured size limit")
            try:
                with open(path, "wb") as bookfile:
                    for chunk in res.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        bookfile.write(chunk)
                        bytes_written += len(chunk)
                        if max_bytes is not None and bytes_written > max_bytes:
                            raise ValueError("Download exceeds the configured size limit")
            except Exception:
                if path.exists():
                    path.unlink()
                raise

        return bytes_written

    def isLoggedIn(self) -> bool:
        return self.__loggedin

    def getDownloadsLeft(self) -> int:
        user_profile: dict = self.getProfile()["user"]
        return user_profile.get("downloads_limit", 10) - user_profile.get("downloads_today", 0)
