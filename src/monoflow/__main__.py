from __future__ import absolute_import, print_function

import shutil

from .core import cli, ENVVAR_PREFIX

if __name__ == "__main__":
    cli(
        auto_envvar_prefix=ENVVAR_PREFIX,
        max_content_width=shutil.get_terminal_size().columns,
    )
