#
# Copyright (c) 2021, 2022 Astroncia <kestraly@gmail.com>
# Copyright (c) 2023 yuki-chan-nya <yukichandev@proton.me>
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
# The Font Awesome pictograms are licensed under the CC BY 4.0 License.
# https://fontawesome.com/
# https://creativecommons.org/licenses/by/4.0/
#
import logging
import gettext
import functools
from yuki_iptv.qt import get_qt_library

qt_library, QtWidgets, QtCore, QtGui, QShortcut = get_qt_library()

logger = logging.getLogger(__name__)
_ = gettext.gettext

# Time to wait for ffmpeg to finish gracefully after sending "q" (ms)
GRACEFUL_STOP_TIMEOUT = 5000


class YukiData:
    ffmpeg_proc = None
    ffmpeg_processes = None
    show_record_exception = None
    # id() of processes stopped by the user, their exit is not an error
    stopped_by_user = set()


def is_ffmpeg_recording():
    """Returns False while the manual recording process is running,
    True once it has finished, None if there is no process.
    (Inverted semantics kept for compatibility with callers.)"""
    if YukiData.ffmpeg_proc is None:
        return None
    if YukiData.ffmpeg_proc.processId() == 0:
        YukiData.ffmpeg_proc = None
        return True
    return False


def _read_tail(data, lines=15):
    try:
        text = bytes(data).decode("utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(text.split("\n")[-lines:])


def exit_handler(proc, exit_code, exit_status):
    stopped_by_user = id(proc) in YukiData.stopped_by_user
    YukiData.stopped_by_user.discard(id(proc))

    crashed = exit_status != QtCore.QProcess.ExitStatus.NormalExit
    if stopped_by_user or (not crashed and exit_code == 0):
        logger.info(f"ffmpeg finished (exit code {exit_code})")
        return

    # ffmpeg exits normally with a non-zero code on connection errors,
    # HTTP 403/404, invalid URL etc. - these must be reported too
    logger.warning(f"ffmpeg failed (exit code {exit_code}, crashed: {crashed})")
    if not YukiData.show_record_exception:
        return
    try:
        standard_output = _read_tail(proc.readAllStandardOutput())
        standard_error = _read_tail(proc.readAllStandardError())
        YukiData.show_record_exception(
            (_("ffmpeg crashed!") if crashed else _("Recording failed!"))
            + "\n"
            + _("exit code:")
            + " "
            + str(exit_code)
            + "\nstdout:\n"
            + standard_output
            + "\nstderr:\n"
            + standard_error
        )
    except Exception:
        logger.exception("Failed to show ffmpeg error")
        YukiData.show_record_exception(_("ffmpeg crashed!"))


def error_handler(proc, error):
    if error == QtCore.QProcess.ProcessError.FailedToStart:
        # finished signal is not emitted in this case
        logger.error("ffmpeg failed to start")
        if YukiData.show_record_exception:
            YukiData.show_record_exception(
                _("ffmpeg could not be started!")
                + "\n"
                + _("Make sure ffmpeg is installed and available in PATH.")
            )


def build_ffmpeg_args(input_url, out_file, user_agent, http_referer):
    arr = ["-nostats", "-hide_banner", "-loglevel", "warning"]
    if input_url.startswith("http://") or input_url.startswith("https://"):
        if user_agent:
            arr += ["-user_agent", user_agent]
        if http_referer:
            # ffmpeg expects every header line to end with CRLF
            arr += ["-headers", http_referer + "\r\n"]
    arr += [
        "-i",
        input_url,
        # Select all streams first, otherwise the negative maps below
        # have no effect and only one audio track is recorded
        "-map",
        "0",
        "-map",
        "-0:s?",
        "-map",
        "-0:d?",
        "-sn",
        "-codec",
        "copy",
        "-acodec",
        "aac",
        "-max_muxing_queue_size",
        "4096",
        out_file,
    ]
    return arr


def record(
    input_url,
    out_file,
    channel_name,
    http_referer,
    get_ua_ref_for_channel,
    is_return=False,
):
    if http_referer == "Referer: ":
        http_referer = ""
    useragent_ref, referer_ref = get_ua_ref_for_channel(channel_name)
    user_agent = useragent_ref
    if referer_ref:
        http_referer = f"Referer: {referer_ref}"
    logger.info(f"Using user agent '{user_agent}' for record channel '{channel_name}'")
    logger.info(f"HTTP headers: '{http_referer}'")

    arr = build_ffmpeg_args(input_url, out_file, user_agent, http_referer)

    proc = QtCore.QProcess()
    # Connect signals before starting the process
    proc.finished.connect(functools.partial(exit_handler, proc))
    proc.errorOccurred.connect(functools.partial(error_handler, proc))

    if not is_return:
        # Do not lose the reference to a still running recording
        if YukiData.ffmpeg_proc and YukiData.ffmpeg_proc.processId() != 0:
            stop_process(YukiData.ffmpeg_proc)
        YukiData.ffmpeg_proc = proc

    proc.start("ffmpeg", arr)

    if is_return:
        return proc


def record_return(
    input_url, out_file, channel_name, http_referer, get_ua_ref_for_channel
):
    return record(
        input_url, out_file, channel_name, http_referer, get_ua_ref_for_channel, True
    )


def stop_process(proc, wait=False):
    """Stops ffmpeg gracefully so the output file is finalized.

    wait=True blocks until ffmpeg exits (used on application exit,
    when timers would never fire).

    QProcess.terminate() does nothing for console applications on Windows,
    so "q" is sent to ffmpeg's stdin first; the process is killed only if
    it does not exit in time.
    """
    if not proc or proc.processId() == 0:
        return
    YukiData.stopped_by_user.add(id(proc))
    try:
        proc.write(b"q")
        proc.closeWriteChannel()
    except Exception:
        logger.exception("Failed to send 'q' to ffmpeg")

    if wait:
        if not proc.waitForFinished(GRACEFUL_STOP_TIMEOUT):
            proc.terminate()
            if not proc.waitForFinished(GRACEFUL_STOP_TIMEOUT):
                proc.kill()
        return

    def force_stop():
        if proc.processId() != 0:
            logger.warning("ffmpeg did not exit in time, terminating")
            proc.terminate()
            QtCore.QTimer.singleShot(
                GRACEFUL_STOP_TIMEOUT,
                lambda: proc.kill() if proc.processId() != 0 else None,
            )

    QtCore.QTimer.singleShot(GRACEFUL_STOP_TIMEOUT, force_stop)


def stop_record(wait=False):
    if YukiData.ffmpeg_proc:
        stop_process(YukiData.ffmpeg_proc, wait)


def init_record(show_exception, ffmpeg_processes):
    YukiData.show_record_exception = show_exception
    YukiData.ffmpeg_processes = ffmpeg_processes
