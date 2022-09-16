#!/usr/bin/python3

from typing import Union
import subprocess
from io import TextIOWrapper
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlretrieve
import argparse
from os import makedirs, scandir
from appdirs import user_cache_dir

import logging as log

APPNAME = 'overdrive2opus'

NOISE_MODEL_URL = 'https://raw.githubusercontent.com/GregorR/rnnoise-models/master/somnolent-hogwash-2018-09-01/sh.rnnn'


# I dont' want to ship this due to unknown license
def _get_noise_model():
    filename = Path(user_cache_dir(APPNAME), 'voice.rnnn')
    log.debug('noise_filename = %r', filename)
    if not filename.exists():
        directory = filename.parent
        makedirs(directory, exist_ok=True)
        log.info('Downloading voice/noise model from %r', NOISE_MODEL_URL)
        urlretrieve(NOISE_MODEL_URL, filename)
    return str(filename)


def _str2bytes(s):
    if isinstance(s, bytes):
        return s
    elif isinstance(s, str):
        return s.encode('utf-8')
    else:
        # hope for the best
        log.warning("Can't convert %r to bytes", s)
        return s


def _time2str(t, precision: int = 3):
    minutes, seconds = divmod(t, 60)
    minutes = round(minutes)
    hours, minutes = divmod(minutes, 60)

    fmt = '%02d:%02d:'

    if precision <= 0:
        fmt += '%02d'
    else:
        fmt += '%0'+str(3+precision)+'.'+str(precision)+'f'

    return fmt % (hours, minutes, seconds)


def _list_files(path, ext=None):
    if ext is None:
        ext = ''
    else:
        if len(ext) > 0 and ext[0] != '.':
            ext = '.' + ext

    files = (Path(f) for f in scandir(path) if f.is_file())

    if ext:
        return [f for f in files if ext == f.suffix]
    else:
        return list(files)


try:
    import progress.bar as progress_bar
    Bar = progress_bar.ShadyBar
except ImportError:
    log.info('No progress bar implementation found. Using fallback.')
    from datetime import datetime

    class Bar:

        def __init__(self, title, max, suffix=''):
            self.__title = title
            self.__max = max
            self.__suffix = suffix
            self.__start = datetime.now()

        def goto(self, n):
            delta = datetime.now() - self.__start
            percent = round(100.*n/self.__max)

            eta_td = 'N/A'
            if percent <= 0:
                eta_td = float('+inf')
            else:
                eta_td = (100./percent - 1)*delta
                eta_td = _time2str(round(eta_td.total_seconds()), precision=0)

            suffix = self.__suffix % {'percent': percent, 'eta_td': eta_td}
            print(self.__title + "\t" + suffix + '\r', end='')

        def finish(self):
            self.goto(self.__max)
            print()


def _ts_from_time(s):
    ret = 0
    for n in s.split(':'):
        ret *= 60
        ret += float(n)

    return ret


def _get_metadata(fname):
    args = [
        'ffprobe',
        '-v',
        'quiet',
        '-print_format',
        'json',
        '-show_format',
        fname
    ]

    with subprocess.Popen(args, stdout=subprocess.PIPE) as process:
        ret = json.load(process.stdout)
        log.debug('Raw metadata = %r', ret)
        return ret


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def get_metadata(fname):
    d = _get_metadata(fname)['format']
    t = d['tags']

    ret = {'file': fname}

    for k in ('title', 'artist', 'genre', 'publisher', 'comment', 'album', 'copyright'):
        ret[k] = t.get(k, None)

    track = _int(t.get('track'))
    title = t.get('title')

    if track is None:
        log.debug(
            'No track information for %r. Guessing from title %r.',
            fname, title
        )
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
        'OverDrive MediaMarkers',
        "<?xml version=\"1.0\" ?>\n<metadata/>"
    )
    root = ET.fromstring(media_markers)
    chapters = []

    for marker in root:
        if 'Marker' == marker.tag:
            name = 'Unknown name'
            time = 0
            for child in marker:
                if 'Name' == child.tag:
                    name = child.text
                if 'Time' == child.tag:
                    time = _ts_from_time(child.text)

            chapters.append((name, time))
        else:
            log.warning('invalid XML data %r', marker)

    ret['chapters'] = chapters

    return ret


def get_folder_metadata(folder):
    files = _list_files(Path(folder), 'mp3')

    image = None
    for img in _list_files(Path(folder), 'jpg'):
        img = img.as_posix()
        # ignore thumbnails
        # TODO Maybe I should just keep the highest resolution image
        if '_thumb' in img:
            continue
        else:
            image = img

    ret = {}

    files = [get_metadata(f) for f in files]

    # order by track number
    files.sort(key=lambda f: int(f['track']))

    # now get the chapter information

    chapters = []
    delta = 0

    for f in files:
        for name, time in f['chapters']:
            chapters.append((name, time + delta))

        delta += f['duration']

    ret['files'] = files
    ret['chapters'] = chapters
    ret['duration'] = delta

    # now get general data

    def _get_field(field):
        for f in files:
            value = f.get(field, None)

            if value is not None:
                return value

        return 'Unknown'

    title = _get_field('album')

    if 'Unknown' == title:
        log.warning('No album information, guessing title')

        title = _get_field('title')
        title = re.sub(
            r'\s*-?\s*Part\s*\d+\s*$',
            '',
            title,
            flags=re.IGNORECASE
        )
        log.info('Title = %r', title)

    ret['title'] = title
    ret['album'] = title + ' - Overdrive'
    ret['image'] = image

    for f in ('artist', 'genre', 'comment', 'publisher', 'copyright'):
        ret[f] = _get_field(f)

    log.debug('Folder metadata for %r = %r', folder, ret)
    return ret


def encode(
        folder: Path,
        opus: Union[Path, None] = None,
        bitrate: float = 15,
        subchapters: bool = False,
        af: str = None,
        progress: bool = True,
        speed: int = 0,
        normalize: int = None,
        isolate_voice: bool = False
        ):

    if speed < -50:
        log.warning('Invalid speed: truncating to -90%')
        speed = -50
    speed_float = 1 + speed/100.0

    folder = Path(folder)
    if opus is None:
        log.warning('Guessing opus filename')
        opus = folder.with_suffix('.opus')

    log.info('Encoding from %s to %s', folder, opus)

    metadata = get_folder_metadata(folder)

    if 0 == len(metadata['files']):
        log.warning('No mp3 files found. Nothing to encode')
        raise FileNotFoundError

    opus_params = [
        'opusenc',
        '--quiet',
        '--ignorelength',
        '--framesize', '60',
        '--downmix-mono',
        '--comp', '10',
        '--vbr', '--bitrate', str(bitrate),
        '--speech',  # override detection
        '--title', _str2bytes(metadata['title']),
        '--artist', _str2bytes(metadata['artist']),
        '--album', _str2bytes(metadata['album']),
        '--genre', _str2bytes(metadata['genre'])
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
        opus_params.extend([
            '--comment',
            ('CHAPTER%02d=' % chapter_n)+_time2str(time/speed_float)
        ])
        opus_params.extend([
            '--comment',
            _str2bytes('CHAPTER%02dNAME=%s' % (chapter_n, name))
        ])

        prev_name = name

    image = metadata['image']
    if image is not None:
        opus_params.extend(['--picture', image])

    opus_params.extend(['-', opus])

    log.debug('opusenc = %r', opus_params)

    ffmpeg_params = ['ffmpeg', '-loglevel', 'quiet', '-hide_banner', '-stats']

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
        filt += ','+af

    ffmpeg_params.extend([
        '-filter_complex', filt,
        '-f', 'wav',
        '-acodec', 'pcm_s16le',
        '-'
    ])

    log.debug('ffmpeg_params = %r', ffmpeg_params)

    log.info(
        '%s files (%s)',
        len(metadata['files']),
        _time2str(metadata['duration'])
    )

    ffmpeg_sub = subprocess.Popen(
        ffmpeg_params,
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    progress_io = TextIOWrapper(ffmpeg_sub.stderr, newline="\r", line_buffering=True)
    opus_sub = subprocess.Popen(
        opus_params,
        stdin=ffmpeg_sub.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if progress:
        bar = Bar(
            'Processing %r' % (metadata['title'],),
            max=metadata['duration']/speed_float,
            suffix='%(percent)d%% [%(eta_td)s]'
        )

        progress_rx = re.compile('\s*time\s*=\s*(\S+)\s*')

    while opus_sub.poll() is None:
        line = progress_io.readline()

        if not progress:
            continue

        m = progress_rx.search(line)
        if m:
            timestr = m.group(1)
            progress_time = _ts_from_time(timestr)
            bar.goto(progress_time)
    bar.finish()
    opus_sub.wait()
    ffmpeg_sub.wait()


parser = argparse.ArgumentParser(
    description='Convert a OverDrive audiobook folder with an opus file '
                'with thumbnail and chapter information'
)
parser.add_argument(
    '--bitrate',
    type=int,
    help='opus bitrate in kbps',
    default=15
)
parser.add_argument(
    '--subchapters',
    action='store_true',
    help='include subchapters'
)
parser.add_argument(
    '--noprogress',
    action='store_true',
    help='do not display encoding progress bar'
)
parser.add_argument(
    '--speed',
    type=int,
    default=0,
    help='speed up or down audio (signed integer %%). Chapters adjusted accordingly'
)
parser.add_argument(
    '--normalize',
    type=int,
    default=None,
    help='%% of max volume for dynamic normalization'
)
parser.add_argument(
    '--isolate_voice',
    action='store_true',
    help='apply filter to isolate voice from background noise'
)
parser.add_argument(
    '--filter',
    type=str,
    help='audio filter for fmmpeg. don\'t use unless you know what you are doing',
    default=None,
    required=False
)
parser.add_argument(
    'folder',
    type=str,
    help='input folder'
)
parser.add_argument(
    'opus_file',
    type=str,
    help='output opus file',
    default=None,
    nargs='?'
)
parser.add_argument(
    '-v',
    '--verbose',
    help='increase output verbosity',
    action='store_true'
)


args = parser.parse_args()

if args.verbose:
    log.basicConfig(level=log.DEBUG)
    log.debug('args = %r', args)
else:
    log.basicConfig(level=log.WARNING)

encode(
    args.folder,
    args.opus_file,
    bitrate=args.bitrate,
    subchapters=args.subchapters,
    af=args.filter,
    progress=not args.noprogress,
    speed=args.speed,
    normalize=args.normalize,
    isolate_voice=args.isolate_voice
)
