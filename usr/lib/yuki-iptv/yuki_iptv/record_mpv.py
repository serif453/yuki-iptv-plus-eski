#
# Copyright (c) 2026 serif453
#
# This file is part of yuki-iptv.
#
# yuki-iptv is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# yuki-iptv is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with yuki-iptv. If not, see <https://www.gnu.org/licenses/>.
#
"""Record while watching, using the player's existing connection.

mpv's "stream-record" option writes the packets coming out of the demuxer
to a file without re-encoding, so no second connection to the provider is
opened. Many IPTV providers allow only one connection per account.
"""

import os
import logging
import gettext
from yuki_iptv.qt import get_qt_library

qt_library, QtWidgets, QtCore, QtGui, QShortcut = get_qt_library()

logger = logging.getLogger(__name__)
_ = gettext.gettext


class MpvRecorder(QtCore.QObject):
    """All methods must be called from the Qt main thread.

    Lifecycle around a channel load (call from the player code):
        recorder.before_load(url)   # before loadfile
        recorder.on_file_loaded()   # on mpv "file-loaded" event

    Loading the same URL again (reconnect) continues the recording in a
    new "_partN" file. mpv would otherwise reopen and truncate the same
    file. Loading a different URL ends the recording.
    """

    # Recording is considered failed if the file does not grow this long (ms)
    STALL_TIMEOUT = 20000
    CHECK_INTERVAL = 2000

    def __init__(
        self,
        player,
        show_exception=None,
        on_stopped=None,
        on_file_changed=None,
        parent=None,
    ):
        super().__init__(parent)
        self.player = player
        self.show_exception = show_exception
        # Called with no arguments when the recording ends by itself
        # (channel change, stall), so the UI can be reset
        self.on_stopped = on_stopped
        # Called with the new path when a new part file is started
        self.on_file_changed = on_file_changed

        self.url = None
        self.out_file = None  # file currently being written
        self._base_file = None
        self._part = 1
        self._pending_part = False
        self._last_size = -1
        self._stalled_ms = 0

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(self.CHECK_INTERVAL)
        self._timer.timeout.connect(self._check_progress)

    @property
    def is_recording(self):
        return self.url is not None

    def start(self, out_file, url):
        if self.is_recording:
            self.stop(notify=False)

        ext = os.path.splitext(out_file)[1].lower()
        if ext not in (".ts", ".mkv"):
            logger.warning(
                f"'{ext}' is not recommended for stream-record, use .ts or .mkv"
            )

        if not self._open(out_file):
            return False
        self.url = url
        self._base_file = out_file
        self._part = 1
        self._pending_part = False
        self._timer.start()
        logger.info(f"mpv recording started: {out_file}")
        return True

    def stop(self, notify=False):
        if not self.is_recording:
            return
        self._timer.stop()
        self._close()
        logger.info(f"mpv recording stopped (url: {self.url})")
        self.url = None
        self.out_file = None
        self._base_file = None
        self._pending_part = False
        if notify and self.on_stopped:
            self.on_stopped()

    def before_load(self, url):
        if not self.is_recording:
            return
        # Always close the file before loadfile, otherwise mpv reopens
        # (truncates) it for the new stream
        self._close()
        if url == self.url:
            logger.info("Same stream is being reloaded, recording will continue")
            self._pending_part = True
            self._stalled_ms = 0
        else:
            logger.info("Channel changed, stopping mpv recording")
            self.stop(notify=True)

    def on_file_loaded(self):
        if not self.is_recording or not self._pending_part:
            return
        self._pending_part = False
        self._part += 1
        root, ext = os.path.splitext(self._base_file)
        new_file = f"{root}_part{self._part}{ext}"
        if self._open(new_file):
            logger.info(f"mpv recording continues in {new_file}")
            if self.on_file_changed:
                self.on_file_changed(new_file)
        else:
            self.stop(notify=True)

    def _open(self, out_file):
        try:
            self.player["stream-record"] = out_file
        except Exception as e:
            logger.exception("Failed to set mpv stream-record")
            self._report(_("Recording could not be started") + f"\n{e}")
            return False
        self.out_file = out_file
        self._last_size = -1
        self._stalled_ms = 0
        return True

    def _close(self):
        try:
            # Empty value closes and finalizes the file
            self.player["stream-record"] = ""
        except Exception:
            logger.exception("Failed to reset mpv stream-record")
        self.out_file = None

    def _check_progress(self):
        if not self.is_recording:
            return
        size = -1
        if self.out_file:
            try:
                size = os.path.getsize(self.out_file)
            except OSError:
                pass

        if size > self._last_size:
            self._last_size = size
            self._stalled_ms = 0
            return

        # While paused mpv keeps reading until the cache is full and then
        # stops, so a file that does not grow is expected
        try:
            if self.player["pause"]:
                self._stalled_ms = 0
                return
        except Exception:
            pass

        self._stalled_ms += self.CHECK_INTERVAL
        if self._stalled_ms >= self.STALL_TIMEOUT:
            path = self.out_file or self._base_file
            logger.warning(f"mpv recording stalled: {path}")
            self.stop(notify=True)
            self._report(_("Recording stopped: no data is being written") + f"\n{path}")

    def _report(self, message):
        if self.show_exception:
            self.show_exception(message)
