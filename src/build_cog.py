#!/usr/bin/env python
'''Build a Cloud Optimized GeoTIFF from a directory of GeoTIFF imagery.

Example:
    python src/build_cog.py /path/to/chix_fire/

The default output for that example is:
    /path/to/chix_fire_cog/chix_fire.tif
'''

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


TIFF_SUFFIXES = {'.tif', '.tiff'}


def find_tiffs(input_dir: Path, recursive: bool) -> list[Path]:
    '''Locates tiffs in input dir'''
    pattern = '**/*' if recursive else '*'
    return sorted(
        path
        for path in input_dir.glob(pattern)
        if path.is_file() and path.suffix.lower() in TIFF_SUFFIXES
    )


def require_command(name: str) -> str:
    '''Checks for dependencies'''
    command = shutil.which(name)
    if not command:
        raise SystemExit(
            f'Missing required command: {name}. '
            'Install GDAL, for example with `conda env update -f environment.yml`.'
        )
    return command


def run_command(command: list[str]) -> None:
    '''Runs GDAL commands as subprocesses '''
    print('Running:', ' '.join(command))
    subprocess.run(command, check=True)


def default_output_dir(input_dir: Path) -> Path:
    '''Make output dir name'''
    return input_dir.parent / f'{input_dir.name}_cog'


def build_vrt(
    gdalbuildvrt: str,
    tiffs: list[Path],
    output_dir: Path,
    resolution: str,
) -> Path:
    '''Builds vrt from tiles in input dir'''
    with tempfile.NamedTemporaryFile(
        'w',
        dir=output_dir,
        encoding='utf-8',
        suffix='.txt',
        delete=False,
    ) as file:
        input_list_path = Path(file.name)
        for path in tiffs:
            file.write(f'{path}\n')

    vrt_path = output_dir / 'mosaic.vrt'
    try:
        run_command(
            [
                gdalbuildvrt,
                '-resolution',
                resolution,
                '-input_file_list',
                str(input_list_path),
                str(vrt_path),
            ]
        )
    finally:
        input_list_path.unlink(missing_ok=True)

    return vrt_path


def build_cog(
    gdal_translate: str,
    source_path: Path,
    output_path: Path,
    compress: str,
    quality: int,
    overwrite: bool,
) -> None:
    '''Builds COG from vrt'''
    command = [
        gdal_translate,
        '-of',
        'COG',
        str(source_path),
        str(output_path),
        '-co',
        f'COMPRESS={compress.upper()}',
        '-co',
        'BIGTIFF=IF_SAFER',
        '-co',
        'BLOCKSIZE=512',
        '-co',
        'RESAMPLING=BILINEAR',
        '-co',
        'NUM_THREADS=ALL_CPUS',
    ]
    if compress.lower() in {'jpeg', 'webp'}:
        command.extend(['-co', f'QUALITY={quality}'])
    if overwrite:
        command.insert(1, '-overwrite')

    run_command(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Build a self-contained COG from TIFF imagery in a directory. '
            'Non-TIFF files in the input directory are ignored.'
        )
    )
    parser.add_argument(
        'input_dir',
        type=Path,
        help='Directory containing source .tif/.tiff imagery.',
    )
    parser.add_argument(
        '--output-dir',
        type=Path,
        help='Output directory. Default: sibling directory named <input_dir>_cog.',
    )
    parser.add_argument(
        '--output-name',
        help='Output COG filename. Default: <input_dir>.tif.',
    )
    parser.add_argument(
        '--recursive',
        action='store_true',
        help='Search for TIFFs recursively under input_dir.',
    )
    parser.add_argument(
        '--resolution',
        choices=['highest', 'lowest', 'average', 'user'],
        default='highest',
        help='Resolution strategy passed to gdalbuildvrt when mosaicking multiple TIFFs.',
    )
    parser.add_argument(
        '--compress',
        default='deflate',
        choices=['deflate', 'lzw', 'zstd', 'jpeg', 'webp', 'none'],
        help='COG compression. Default: deflate.',
    )
    parser.add_argument(
        '--quality',
        type=int,
        default=90,
        help='JPEG/WEBP quality when using lossy compression. Default: 90.',
    )
    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Overwrite an existing output COG.',
    )
    parser.add_argument(
        '--keep-vrt',
        action='store_true',
        help='Keep mosaic.vrt in the output directory when multiple TIFFs are used.',
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        raise SystemExit(f'Input directory does not exist: {input_dir}')

    tiffs = find_tiffs(input_dir, args.recursive)
    if not tiffs:
        raise SystemExit(f'No .tif or .tiff files found in {input_dir}')

    output_dir = (args.output_dir or default_output_dir(input_dir)).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = args.output_name or f'{input_dir.name}.tif'
    output_path = output_dir / output_name

    if output_path.exists() and not args.overwrite:
        raise SystemExit(
            f'Output already exists: {output_path}\n'
            'Pass --overwrite if you want to replace it.'
        )

    gdal_translate = require_command('gdal_translate')
    source_path = tiffs[0]
    vrt_path = None

    if len(tiffs) > 1:
        gdalbuildvrt = require_command('gdalbuildvrt')
        vrt_path = build_vrt(gdalbuildvrt, tiffs, output_dir, args.resolution)
        source_path = vrt_path

    build_cog(
        gdal_translate=gdal_translate,
        source_path=source_path,
        output_path=output_path,
        compress=args.compress,
        quality=args.quality,
        overwrite=args.overwrite,
    )

    if vrt_path and not args.keep_vrt:
        vrt_path.unlink(missing_ok=True)

    print(f'Found {len(tiffs):,} TIFF file(s)')
    print(f'Wrote COG: {output_path}')


if __name__ == '__main__':
    main()
