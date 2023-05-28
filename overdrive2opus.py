#!/usr/bin/python3

from typing import Optional, Union
import logging as log
import subprocess
from io import TextIOWrapper
import json
import re
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from urllib.request import urlretrieve
import argparse
from appdirs import user_cache_dir

from rich.logging import RichHandler
from rich.traceback import install as traceback_install
from rich.progress import (Progress, BarColumn, TimeRemainingColumn, TextColumn, Column)


APPNAME = 'overdrive2opus'

NOISE_MODEL_URL = (
    'https://raw.githubusercontent.com/GregorR/'
    'rnnoise-models/master/somnolent-hogwash-2018-09-01/sh.rnnn'
)


# I dont' want to ship this due to unknown license
def _get_noise_model() -> str:
    filename = Path(user_cache_dir(APPNAME), 'voice.rnnn')
    log.debug('noise_filename = %r', filename)
    if not filename.exists():
        directory = filename.parent
        directory.mkdir(exist_ok=True, parents=True)
        log.info('Downloading voice/noise model from %r', NOISE_MODEL_URL)
        urlretrieve(NOISE_MODEL_URL, filename)
    return str(filename)


def _str2bytes(s: str | bytes) -> bytes:
    if isinstance(s, bytes):
        return s
    elif isinstance(s, str):
        return s.encode('utf-8')
    else:
        # hope for the best
        log.warning("Can't convert %r to bytes", s)
        return s


def _time2str(t: float, precision: int = 3) -> str:
    minutes, seconds = divmod(t, 60)
    minutes = round(minutes)
    hours, minutes = divmod(minutes, 60)

    fmt = '%02d:%02d:'

    if precision <= 0:
        fmt += '%02d'
    else:
        fmt += '%0' + str(3 + precision) + '.' + str(precision) + 'f'

    return fmt % (hours, minutes, seconds)


def _list_files(path: Path, ext: Optional[str] = None, case=False) -> list[Path]:
    if ext is None:
        ext = ''
    else:
        if len(ext) > 0 and ext[0] != '.':
            ext = '.' + ext

    files = (f for f in path.iterdir() if f.is_file())

    if ext:
        if case:
            return [f for f in files if ext == f.suffix]
        else:
            ext = ext.lower()
            return [f for f in files if ext == f.suffix.lower()]
    return list(files)


def _ts_from_time(s: str) -> float:
    ret: float = 0
    for n in s.split(':'):
        ret *= 60
        ret += float(n)

    return ret


def _get_metadata(fname: Path):
    args: list[Union[str | Path]] = [
        'ffprobe',
        '-v',
        'quiet',
        '-print_format',
        'json',
        '-show_format',
        fname,
    ]

    with subprocess.Popen(args, stdout=subprocess.PIPE) as process:
        if process.stdout is None:
            raise RuntimeError("Error in getting data from ffmpeg")
        ret = json.load(process.stdout)
        log.debug('Raw metadata = %r', ret)
        return ret


def _int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def get_metadata(fname: Path) -> dict:
    d = _get_metadata(fname)['format']
    t = d['tags']

    ret: dict = {'file': fname}

    for k in ('title', 'artist', 'genre', 'publisher', 'comment', 'album', 'copyright'):
        _val = t.get(k, None)

        # comments are usually full of xml/html entities
        if _val is not None:
            _val = unescape(_val)

        ret[k] = _val

    track = _int(t.get('track'))
    title = t.get('title')

    if track is None:
        log.debug('No track information for %r. Guessing from title %r.', fname, title)
        if title is None:
            raise KeyError("No title. Can't determine track")

        rx = re.compile(r'-\s*Part\s*(\d+)', re.IGNORECASE)
        m = rx.search(title)

        if m:
            track = int(m.group(1))
        else:
            log.error("Couldn't determine track information")
            raise LookupError('No track information')

    ret['track'] = track
    ret['duration'] = float(d['duration'])

    # now for the OverDrive chapter information

    media_markers = t.get(
        'OverDrive MediaMarkers', "<?xml version=\"1.0\" ?>\n<metadata/>"
    )
    root = ET.fromstring(media_markers)
    chapters = []

    for marker in root:
        if 'Marker' == marker.tag:
            name: str = 'Unknown name'
            time: float = 0
            for child in marker:
                if 'Name' == child.tag and child.text is not None:
                    name = child.text
                if 'Time' == child.tag and child.text is not None:
                    time = _ts_from_time(child.text)

            chapters.append((name, time))
        else:
            log.warning('invalid XML data %r', marker)

    ret['chapters'] = chapters

    return ret


def get_folder_metadata(folder: Path):
    files = _list_files(Path(folder), 'mp3')

    # TODO look at the actual image dimensions and not just file size
    # just pick the largest image as the correct one

    try:
        image = max((f.stat().st_size, f) for f in _list_files(folder, 'jpg'))[1]
    except ValueError:
        image = None

    ret: dict = {}

    files_meta = [get_metadata(f) for f in files]

    # order by track number
    files_meta.sort(key=lambda f: int(f['track']))

    # now get the chapter information

    chapters = []
    delta = 0

    for f in files_meta:
        for name, time in f['chapters']:
            chapters.append((name, time + delta))

        delta += f['duration']

    ret['files'] = files_meta
    ret['chapters'] = chapters
    ret['duration'] = delta

    # now get general data

    def _get_field(field: str) -> str:
        for f in files_meta:
            value = f.get(field, None)

            if value is not None:
                return value

        return 'Unknown'

    title = _get_field('album')

    if 'Unknown' == title:
        log.warning('No album information, guessing title')

        title = _get_field('title')
        title = re.sub(r'\s*-?\s*Part\s*\d+\s*$', '', title, flags=re.IGNORECASE)
        log.info('Title = %r', title)

    ret['title'] = title
    ret['album'] = title + ' - Overdrive'
    ret['image'] = image

    for field in ('artist', 'genre', 'comment', 'publisher', 'copyright'):
        ret[field] = _get_field(field)

    log.debug('Folder metadata for %r = %r', folder, ret)
    return ret


def encode(
    folder: Path,
    opus: Optional[Path] = None,
    bitrate: float = 15,
    subchapters: bool = False,
    af: str | None = None,
    progress: bool = True,
    speed: int = 0,
    normalize: Optional[int] = None,
    isolate_voice: bool = False,
) -> None:
    if speed < -99:
        log.warning('Invalid speed: truncating to -99%')
        speed = -50
    speed_float = 1 + speed / 100.0

    folder = Path(folder)
    if opus is None:
        log.warning('Guessing opus filename')
        opus = folder.with_suffix('.opus')

    log.info('Encoding from %s to %s', folder, opus)

    metadata = get_folder_metadata(folder)

    if 0 == len(metadata['files']):
        log.warning('No mp3 files found. Nothing to encode')
        raise FileNotFoundError

    opus_params: list[str | bytes] = [
        'opusenc',
        '--quiet',
        '--ignorelength',
        '--framesize',
        '60',
        '--downmix-mono',
        '--comp',
        '10',
        '--vbr',
        '--bitrate',
        str(bitrate),
        '--speech',  # override detection
        '--title',
        _str2bytes(metadata['title']),
        '--artist',
        _str2bytes(metadata['artist']),
        '--album',
        _str2bytes(metadata['album']),
        '--genre',
        _str2bytes(metadata['genre']),
    ]

    def _add_comment(k, s):
        # TODO Do I need to escape = or space?
        opus_params.extend(['--comment', _str2bytes(f"{k}={metadata[s]}")])

    _add_comment('description', 'comment')
    _add_comment('publisher', 'publisher')
    _add_comment('copyright', 'copyright')

    chapter_n = 0
    prev_name = None
    for name, time in metadata['chapters']:
        if not subchapters:
            # cleanup spurious sub chapters
            if len(name) and name[0].isspace():
                log.info('Ignoring subchapter %r due to indent', name)
                continue
            elif re.search(r'\s+\([0-9:]+\)\s*$', name):
                log.info('Ignoring subchapter %r due to timestamp', name)
                continue
            elif prev_name is not None and prev_name == name:
                log.info('Ignoring subchapter %r due to repeated chapter name', name)
                continue

        chapter_n += 1
        opus_params.extend(
            ['--comment', ('CHAPTER%02d=' % chapter_n) + _time2str(time / speed_float)]
        )
        opus_params.extend(
            ['--comment', _str2bytes('CHAPTER%02dNAME=%s' % (chapter_n, name))]
        )

        prev_name = name

    image = metadata['image']
    if image is not None:
        opus_params.extend(['--picture', image])

    opus_params.extend(['-', str(opus)])

    log.debug('opusenc = %r', opus_params)

    ffmpeg_params = [
        'ffmpeg',
        '-loglevel',
        'quiet',
        '-hide_banner',
        '-stats',
        '-stats_period',
        '1',
    ]

    filt = ''
    for n, f in enumerate(metadata['files']):
        log.debug('Appending file %r to input', f)
        ffmpeg_params.extend(['-i', f['file']])
        filt += f"[{n}:a]"

    # now for the complex filter
    filt += f"concat=n={n+1}:v=0:a=1"

    if isolate_voice:
        noise_filename = _get_noise_model()
        filt += f',arnndn=m={noise_filename}'

    if normalize is not None:
        if normalize > 100:
            normalize = 100
        elif normalize < 0:
            normalize = 0

        # "widest" parameters possible
        audio_peak = normalize / 100.0
        audio_framelen = 8000
        audio_gausssize = 301
        audio_correctdc = 1

        filt += f',dynaudnorm=peak={audio_peak}:framelen={audio_framelen}:gausssize={audio_gausssize}:correctdc={audio_correctdc}'

    if 0 != speed:
        log.info('Adding speedup filter %r', speed_float)
        filt += ',atempo=%f' % (speed_float,)

    if af is not None:
        log.info('Adding filter %r', af)
        filt += ',' + af

    ffmpeg_params.extend(
        ['-filter_complex', filt, '-f', 'wav', '-acodec', 'pcm_s16le', '-']
    )

    log.debug('ffmpeg_params = %r', ffmpeg_params)

    log.info('%s files (%s)', len(metadata['files']), _time2str(metadata['duration']))

    ffmpeg_sub = subprocess.Popen(
        ffmpeg_params,
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if ffmpeg_sub.stderr is None:
        raise RuntimeError("Error running ffmpeg encoder")

    progress_io = TextIOWrapper(ffmpeg_sub.stderr, newline="\r", line_buffering=True)
    opus_sub = subprocess.Popen(
        opus_params,
        stdin=ffmpeg_sub.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    with Progress(
            TextColumn(f"[bold blue]{metadata['title']}", justify="right", table_column=Column(ratio=1)),
            BarColumn(bar_width=None, table_column=Column(ratio=2)),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            TimeRemainingColumn(),
        ) as bar:
        if progress:

            total = metadata['duration'] / speed_float
            task = bar.add_task('Encoding', total=total)
            bar.update(task, total=total)
            bar.print("[bold]Processing")

            progress_rx = re.compile(r'\s*time\s*=\s*(\S+)\s*')

        _progress_time = 0.0
        while opus_sub.poll() is None:
            line = progress_io.readline()

            if not progress:
                continue

            m = progress_rx.search(line)
            if m:
                timestr = m.group(1)
                progress_time = _ts_from_time(timestr)
                delta = progress_time - _progress_time
                if delta > 0:
                    _progress_time = progress_time
                    bar.update(task, advance=delta)
        opus_sub.wait()
        ffmpeg_sub.wait()
        if progress:
            bar.print("[bold green]:thumbs_up:")


parser = argparse.ArgumentParser(
    description='Convert a OverDrive audiobook folder with an opus file '
    'with thumbnail and chapter information'
)
parser.add_argument('--bitrate', type=int, help='opus bitrate in kbps', default=15)
parser.add_argument('--subchapters', action='store_true', help='include subchapters')
parser.add_argument(
    '--noprogress', action='store_true', help='do not display encoding progress bar'
)
parser.add_argument(
    '--speed',
    type=int,
    default=0,
    help='speed up or down audio (signed integer %%). Chapters adjusted accordingly',
)
parser.add_argument(
    '--normalize',
    type=int,
    default=None,
    help='%% of max volume for dynamic normalization',
)
parser.add_argument(
    '--isolate_voice',
    action='store_true',
    help='apply filter to isolate voice from background noise',
)
parser.add_argument(
    '--filter',
    type=str,
    help='audio filter for fmmpeg. don\'t use unless you know what you are doing',
    default=None,
    required=False,
)
parser.add_argument('folder', type=str, help='input folder')
parser.add_argument(
    'opus_file', type=str, help='output opus file', default=None, nargs='?'
)
parser.add_argument(
    '-v', '--verbose', help='increase output verbosity', action='store_true'
)


args = parser.parse_args()

if args.verbose:
    traceback_install(show_locals=True)
    log.basicConfig(level=log.DEBUG, handlers=[RichHandler(rich_tracebacks=True)])
    log.debug('args = %r', args)
else:
    log.basicConfig(level=log.WARNING, handlers=[RichHandler(rich_tracebacks=False)]
)

encode(
    args.folder,
    args.opus_file,
    bitrate=args.bitrate,
    subchapters=args.subchapters,
    af=args.filter,
    progress=not args.noprogress,
    speed=args.speed,
    normalize=args.normalize,
    isolate_voice=args.isolate_voice,
)
