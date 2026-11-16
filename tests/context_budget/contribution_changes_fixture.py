"""Saved contribution-change fixtures (from the former replay experiment script)."""

import shutil


def prepare(work):
    context = work / 'context'
    context.mkdir()
    for name, size in {'a/shrink.BIN': 12, 'a/nested/grow.bin': 2, 'ab/same.bin': 4,
                       'gone/old.old': 8, 'only-before/zero.z': 0, 'archive.tar.gz': 2,
                       '.hidden': 2, 'name.': 0, 'root': 0, 'ignored/keep.bin': 3,
                       'prov/same.same': 6, 'a/control.ctl': 40, 'a/excluded.skip': 90}.items():
        put(context, name, size)
    (context / 'empty-before').mkdir()
    (context / 'a/link.link').symlink_to('missing')
    (context / 'alias').symlink_to('a', target_is_directory=True)
    rules = context / 'rules.rules'
    rules.write_text('a/excluded.skip\nignored\n!ignored/keep.bin\n!prov/same.same\n')
    return context, rules


def put(context, name, size):
    path = context / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)


def advance(context, rules):
    for name, size in {'a/shrink.BIN': 2, 'a/nested/grow.bin': 12, 'new/new.new': 8,
                       'only-after/zero.Z': 0, 'a/nested/zero': 0, 'archive.tar.gz': 5,
                       'ignored/keep.bin': 5, 'a/control.ctl': 400, 'a/excluded.skip': 900}.items():
        put(context, name, size)
    for directory in ('gone', 'only-before', 'empty-before'):
        shutil.rmtree(context / directory)
    (context / 'empty-after').mkdir()
    (context / 'a/link.link').unlink()
    (context / 'a/link.link').symlink_to('different-missing')
    rules.write_text('# provenance shifts without payload changes\n' + rules.read_text())


# Independently calculated before/after byte/file counts, in required output order.
DIRECTORIES = [('a/nested', 2, 1, 12, 2), ('gone', 8, 1, 0, 0), ('new', 0, 0, 8, 1),
               ('ignored', 3, 1, 5, 1), ('a', 14, 2, 14, 3),
               ('only-after', 0, 0, 0, 1), ('only-before', 0, 1, 0, 0)]
EXTENSIONS = [('.bin', 9, 3, 21, 3), ('.BIN', 12, 1, 2, 1), ('.new', 0, 0, 8, 1),
              ('.old', 8, 1, 0, 0), ('.gz', 2, 1, 5, 1), ('.Z', 0, 0, 0, 1),
              ('.z', 0, 1, 0, 0), ('<none>', 2, 3, 2, 4)]


def expected(n):
    return {dimension: [{key: name, 'baseline_included_payload_bytes': b,
                         'baseline_included_payload_files': bf, 'current_included_payload_bytes': c,
                         'current_included_payload_files': cf, 'included_payload_bytes_delta': c-b,
                         'included_payload_files_delta': cf-bf}
                        for name, b, bf, c, cf in values[:n]]
            for dimension, key, values in (('directories', 'path', DIRECTORIES),
                                           ('extensions', 'extension', EXTENSIONS))}


def budget_flags(states):
    total, directory, suffix, pair = states
    return ['--max-bytes', 44 - (not total), '--max-dir-bytes', f'ignored={5 - (not directory)}',
            '--max-dir-bytes', 'only-after=0', '--max-extension-bytes', f'.gz={5 - (not suffix)}',
            '--max-extension-bytes', '.absent=0', '--max-dir-extension-bytes', 'a/nested',
            f'.bin={12 - (not pair)}', '--max-dir-extension-bytes', 'only-after', '.Z=0']
