#!/usr/bin/python3

import subprocess
import json
import re
import xml.etree.ElementTree as ET
from glob import glob
from pathlib import PurePath as Path
import argparse

import logging as log

log.basicConfig(level=log.INFO)


def _time2str(t):
    minutes, seconds = divmod(t, 60)
    minutes = round(minutes)
    hours, minutes = divmod(minutes, 60)

    return f'{hours:02}:{minutes:02}:{seconds:06.3f}'


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

    if track is None:
        rx = re.compile(r'-\s*Part\s*(\d+)', re.IGNORECASE)
        m = rx.search(t['title'])

        if m:
            track = int(m.group(1))

    ret['track'] = track
    ret['duration'] = float(d['duration'])

    # now for the OverDrive chapter information

    root = ET.fromstring(t['OverDrive MediaMarkers'])
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
    files = glob(str(Path(Path(folder), Path('*.mp3'))), recursive=False)

    image = None
    for img in glob(str(Path(Path(folder), Path('*.jpg'))), recursive=False):
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
        title = re.sub(r'\s*-?\s*Part\s*\d+\s*$', '', title, flags=re.IGNORECASE)
        log.info('Title = %r', title)

    ret['title'] = title
    ret['album'] = title + ' - Overdrive'
    ret['image'] = image

    for f in ('artist', 'genre', 'comment', 'publisher', 'copyright'):
        ret[f] = _get_field(f)

    log.debug('Folder metadata for %r = %r', folder, ret)
    return ret


def encode(folder, opus=None, bitrate: float = 15, subchapters: bool = False, af: str = None):
    folder = Path(folder)
    if opus is None:
        log.warning('Guessing opus filename')
        opus = Path(str(folder)+'.opus')

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
        '--title', metadata['title'],
        '--artist', metadata['artist'],
        '--album', metadata['album'],
        '--genre', metadata['genre']
    ]

    def _add_comment(k, s):
        # TODO Do I need to escape = or space?
        opus_params.extend(['--comment', f"{k}={metadata[s]}"])

    _add_comment('description', 'comment')
    _add_comment('publisher', 'publisher')
    _add_comment('copyright', 'copyright')

    n = 0
    for name, time in metadata['chapters']:
        if not subchapters:
            # cleanup spurious sub chapters
            if len(name) and name[0].isspace():
                log.info('Ignoring subchapter %r due to indent', name)
                continue
            elif re.search(r'\s+\([0-9:]+\)\s*$', name):
                log.info('Ignoring subchapter %r due to timestamp', name)
                continue

        n += 1
        opus_params.extend([
            '--comment',
            ('CHAPTER%02d=' % n)+_time2str(time)
        ])
        opus_params.extend([
            '--comment',
            'CHAPTER%02dNAME=%s' % (n, name)
        ])

    opus_params.extend(['-', str(opus)])

    image = metadata['image']
    if image is not None:
        opus_params.extend(['--picture', image])

    log.debug('opusenc = %r', opus_params)

    ffmpeg_params = ['ffmpeg', '-loglevel', 'quiet', '-hide_banner']

    filt= ''
    for n, f in enumerate(metadata['files']):
        log.debug('Appending file %r to input', f)
        ffmpeg_params.extend(['-i', f['file']])
        filt += f"[{n}:a]"

    # now for the complex filter
    filt += f"concat=n={n+1}:v=0:a=1"

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

    log.info('%s files (%s)', len(metadata['files']), _time2str(metadata['duration']))

    ffmpeg_sub = subprocess.Popen(ffmpeg_params, stdout=subprocess.PIPE, stdin=subprocess.PIPE)
    opus_sub = subprocess.Popen(opus_params, stdin=ffmpeg_sub.stdout)

    ffmpeg_sub.stdout.close()
    opus_sub.communicate()
    opus_sub.wait()
    ffmpeg_sub.wait()



parser = argparse.ArgumentParser(description='Convert a OverDrive audiobook folder with an opus file with thumbnail and chapter information')
parser.add_argument('--bitrate', type=int, help='opus bitrate in kbps', default=15)
parser.add_argument('--filter', type=str, help='audio filter for fmmpeg', default=None, required=False)
parser.add_argument('--subchapters', action='store_true', help='include subchapters')
parser.add_argument('folder', type=str, help='input folder')
parser.add_argument('opus_file', type=str, help='output opus file', default=None, nargs='?')

args = parser.parse_args()
log.debug('args = %r', args)

encode(args.folder, args.opus_file, bitrate=args.bitrate, subchapters=args.subchapters, af=args.filter)
