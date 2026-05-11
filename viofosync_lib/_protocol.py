"""HTTP protocol layer for talking to the Viofo dashcam.

Listing endpoints (XML and HTML scrape), HEAD probes, and the
chunked atomic byte downloader.

The module-level ``socket_timeout`` and ``max_download_attempts``
globals are intentionally exposed at module scope: the wrapper
:func:`viofosync_lib.download_file_with` mutates them around a
single download call to apply per-request overrides.
"""
from __future__ import annotations

import datetime
import http.client
import logging
import os
import re
import socket
import tempfile
import time
import urllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ._archive import Recording, downloaded_filename_re, get_filepath

logger = logging.getLogger("viofosync_lib.protocol")

# Tunables (mutated by viofosync_lib.download_file_with).
socket_timeout = 10.0
DEFAULT_DOWNLOAD_ATTEMPTS = 1
max_download_attempts = DEFAULT_DOWNLOAD_ATTEMPTS
RETRY_BACKOFF = 5  # seconds, multiplied by attempt number


def parse_viofo_datetime(time_str):
    """Parse the datetime string from Viofo's format."""
    return datetime.datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")


def get_dashcam_filenames(
    base_url,
    *,
    include_normal=True,
    include_ro=True,
):
    """Gets the recording filenames from the Viofo dashcam.

    By default returns both normal and read-only (locked)
    recordings. Pass ``include_normal=False`` or
    ``include_ro=False`` to restrict the listing to a single
    kind."""
    try:
        url = f"{base_url}/?custom=1&cmd=3015&par=1"
        request = urllib.request.Request(url)
        response = urllib.request.urlopen(request)

        if response.getcode() != 200:
            raise RuntimeError(
                f"Error response from {base_url}; "
                f"status code: {response.getcode()}"
            )

        xml_data = response.read().decode('utf-8')
        root = ET.fromstring(xml_data)

        recordings = []
        for file_elem in root.findall(".//File"):
            attr = int(file_elem.find("ATTR").text)
            is_ro = (attr == 33)
            if is_ro and not include_ro:
                continue
            if not is_ro and not include_normal:
                continue
            name = file_elem.find("NAME").text
            filepath = file_elem.find("FPATH").text
            size = int(file_elem.find("SIZE").text)
            timecode = int(file_elem.find("TIMECODE").text)
            ts = parse_viofo_datetime(
                file_elem.find("TIME").text
            )
            recording = Recording(
                name, filepath, size, timecode, ts, attr
            )
            recordings.append(recording)

        logger.info(f"Found {len(recordings)} recordings on dashcam")
        return recordings
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Cannot obtain recordings from {base_url}: {e}"
        ) from e
    except socket.timeout as e:
        raise UserWarning(
            f"Timeout communicating with dashcam at "
            f"{base_url}: {e}"
        ) from e
    except http.client.RemoteDisconnected as e:
        raise UserWarning(
            f"Dashcam disconnected without response; "
            f"address: {base_url}: {e}"
        ) from e
    except ET.ParseError as e:
        raise RuntimeError(
            f"Error parsing XML response from dashcam: {e}"
        ) from e


# HTML directory listing regex.
html_file_re = re.compile(
    r'<a href="(?P<filepath>[^"]+\.MP4)">'
    r'<b>(?P<filename>[^<]+)</b></a>'
    r'<td align=right>\s*(?P<size>[\d.]+)\s*(?P<unit>[KMGT]?B)',
    re.IGNORECASE,
)

# Directories to scrape on the dashcam.
DCIM_DIRS = ["/DCIM/Movie", "/DCIM/Movie/Parking"]
DCIM_DIRS_RO = ["/DCIM/Movie/RO"]

def parse_html_size(size_str, unit):
    """Converts '102.00 MB' style size to bytes."""
    multipliers = {
        "B": 1, "KB": 1 << 10, "MB": 1 << 20,
        "GB": 1 << 30, "TB": 1 << 40,
    }
    return int(float(size_str) * multipliers.get(unit, 1))


def get_dashcam_filenames_html(
    base_url,
    *,
    include_normal=True,
    include_ro=True,
):
    """Gets recordings by scraping the HTML directory listings.

    Much faster than the XML API on cameras with many files.
    By default scrapes both the normal and the read-only
    folders; pass the relevant flag to restrict the listing."""
    dirs = []
    if include_normal:
        dirs += DCIM_DIRS
    if include_ro:
        dirs += DCIM_DIRS_RO
    recordings = []

    for dir_path in dirs:
        url = f"{base_url}{dir_path}"
        try:
            with urllib.request.urlopen(
                url, timeout=socket_timeout
            ) as resp:
                if resp.getcode() != 200:
                    logger.warning(
                        f"HTTP {resp.getcode()} for {url}, "
                        f"skipping"
                    )
                    continue
                html = resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.debug(
                    f"Directory not found: {dir_path}"
                )
                continue
            raise
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach dashcam at {base_url}: {e}"
            ) from e
        except socket.timeout as e:
            raise UserWarning(
                f"Timeout communicating with dashcam at "
                f"{base_url}: {e}"
            ) from e

        for m in html_file_re.finditer(html):
            filepath = m.group("filepath")
            filename = m.group("filename")
            size = parse_html_size(
                m.group("size"), m.group("unit").upper()
            )

            # Always extract datetime from the filename
            # (the actual recording timestamp).
            fm = downloaded_filename_re.search(filename)
            if not fm:
                logger.warning(
                    f"Cannot parse date from filename: "
                    f"{filename}, skipping"
                )
                continue

            ts = datetime.datetime(
                int(fm.group("year")),
                int(fm.group("month")),
                int(fm.group("day")),
                int(fm.group("hour")),
                int(fm.group("minute")),
                int(fm.group("second")),
            )

            recordings.append(Recording(
                filename, filepath, size, None, ts, None
            ))

    logger.info(
        f"Found {len(recordings)} recordings on dashcam "
        f"(HTML mode)"
    )
    return recordings


def get_remote_size(url, timeout):
    """HEAD request to get Content-Length of a remote file."""
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        cl = resp.getheader("Content-Length")
    return int(cl) if cl and cl.isdigit() else None


def human_size(num_bytes):
    """Returns human-readable size string, e.g. '325.1 MB'."""
    if num_bytes == 0:
        return "0 B"
    for factor, suffix in [(1 << 30, "GB"), (1 << 20, "MB"),
                           (1 << 10, "KB"), (1, "B")]:
        if num_bytes >= factor:
            return f"{num_bytes / factor:.1f} {suffix}"
    return f"{num_bytes} B"


def human_speed(num_bytes, elapsed):
    """Returns human-readable speed string, e.g. '27.1 MB/s'."""
    bps = num_bytes / max(elapsed, 1e-9)
    if bps == 0:
        return "0 B/s"
    for factor, suffix in [(1 << 30, "GB/s"), (1 << 20, "MB/s"),
                           (1 << 10, "KB/s"), (1, "B/s")]:
        if bps >= factor:
            return f"{bps / factor:.1f} {suffix}"
    return f"{bps:.1f} B/s"


def download_file(base_url, recording, destination, group_name,
                  progress_sink=None, cancel_check=None):
    """Downloads a file from the Viofo dashcam to the destination.

    Returns (downloaded: bool, speed_str: str|None).
    Uses HEAD to check size, retries up to max_download_attempts,
    and verifies integrity after download.

    Optional args (used by the web UI):
      progress_sink: object with item_started/item_progress/
        item_finished methods; see viofosync_lib.ProgressSink.
      cancel_check: callable returning True if the download
        should be aborted (e.g. reachability lost, user stopped).
    """
    sink = progress_sink
    if group_name:
        group_filepath = os.path.join(destination, group_name)
        if not os.path.exists(group_filepath):
            os.makedirs(group_filepath)
        elif not os.path.isdir(group_filepath):
            raise RuntimeError(
                f"Not a directory: {group_filepath}"
            )
        elif not os.access(group_filepath, os.W_OK):
            raise RuntimeError(
                f"Not writable: {group_filepath}"
            )

    dest_filepath = get_filepath(
        destination, group_name, recording.filename
    )

    # Build download URL — strip drive letter (A: for SD, B: for SSD)
    # and normalise path separators.
    cleaned = re.sub(r'^[A-Z]:', '', recording.filepath).replace(
        '\\', '/'
    )
    url = f"{base_url}/{cleaned.lstrip('/')}"

    # Check expected size via HEAD.
    try:
        expected_size = get_remote_size(url, socket_timeout)
    except Exception:
        expected_size = None

    # Skip if already downloaded and size matches.
    if os.path.exists(dest_filepath):
        local_size = os.path.getsize(dest_filepath)
        if expected_size is not None:
            if local_size == expected_size:
                logger.debug(
                    f"Skipping {recording.filename} "
                    f"({human_size(local_size)})"
                )
                return False, None
            # Size mismatch — re-download.
            logger.info(
                f"Size mismatch for {recording.filename} "
                f"({human_size(local_size)}/"
                f"{human_size(expected_size)}), "
                f"re-downloading"
            )
        elif recording.size is not None:
            if local_size == recording.size:
                logger.debug(
                    f"Skipping {recording.filename} "
                    f"({human_size(local_size)})"
                )
                return False, None
            logger.info(
                f"Size mismatch for {recording.filename} "
                f"({human_size(local_size)}/"
                f"{human_size(recording.size)}), "
                f"re-downloading"
            )
        else:
            logger.debug(
                f"Already downloaded: {recording.filename}"
            )
            return False, None

    # Atomic download with tempfile + retries.
    dest_dir = os.path.dirname(dest_filepath)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=dest_dir,
        prefix=f".{recording.filename}.",
        suffix=".part",
    )
    os.close(tmp_fd)

    if sink is not None:
        sink.item_started(recording.filename, expected_size)

    try:
        for attempt in range(1, max_download_attempts + 1):
            try:
                start = time.perf_counter()
                bytes_done = 0
                last_emit = start
                with urllib.request.urlopen(
                    url, timeout=socket_timeout
                ) as resp, open(tmp_path, "wb") as out:
                    while True:
                        if cancel_check is not None and cancel_check():
                            raise UserWarning(
                                "Download cancelled"
                            )
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        bytes_done += len(chunk)
                        if sink is not None:
                            now = time.perf_counter()
                            if now - last_emit >= 0.25:
                                speed = bytes_done / max(
                                    now - start, 1e-9
                                )
                                sink.item_progress(
                                    recording.filename,
                                    bytes_done,
                                    expected_size,
                                    speed,
                                )
                                last_emit = now
                elapsed = time.perf_counter() - start
            except Exception as e:
                logger.warning(
                    f"Download attempt {attempt} failed for "
                    f"{recording.filename}: {e}"
                )
                if attempt < max_download_attempts:
                    time.sleep(RETRY_BACKOFF * attempt)
                continue

            actual_size = os.path.getsize(tmp_path)

            # Verify integrity.
            if (expected_size is not None
                    and actual_size != expected_size):
                logger.warning(
                    f"Incomplete download of "
                    f"{recording.filename}: "
                    f"{human_size(actual_size)}/"
                    f"{human_size(expected_size)}"
                )
                if attempt < max_download_attempts:
                    time.sleep(RETRY_BACKOFF * attempt)
                continue

            # Success — atomic move into place.
            os.replace(tmp_path, dest_filepath)
            size_str = human_size(actual_size)
            speed_str = human_speed(actual_size, elapsed)
            logger.info(
                f"Downloaded {recording.filename}: "
                f"{size_str} in {elapsed:.1f}s ({speed_str})"
            )
            if sink is not None:
                sink.item_finished(
                    recording.filename, True, None, actual_size
                )
            return True, speed_str

        # All attempts exhausted.
        logger.error(
            f"Failed to download {recording.filename} "
            f"after {max_download_attempts} attempts"
        )
        if sink is not None:
            sink.item_finished(
                recording.filename, False,
                "max attempts exhausted", None,
            )
        return False, None
    except socket.timeout as e:
        raise UserWarning(
            f"Timeout communicating with dashcam at "
            f"{base_url}: {e}"
        ) from e
    except http.client.RemoteDisconnected:
        logger.warning(
            f"Remote end closed connection for "
            f"{recording.filename}; ignoring."
        )
        return False, None
    finally:
        # Clean up temp file if it still exists.
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
