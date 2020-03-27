"""
Utility module to manipulate queries
"""

# Copyright (C) 2020 The Psycopg Team

import re
from collections.abc import Sequence, Mapping

from .. import exceptions as exc


def query2pg(query, vars, codec):
    """
    Convert Python query and params into something Postgres understands.

    - Convert Python placeholders (``%s``, ``%(name)s``) into Postgres
      format (``$1``, ``$2``)
    - placeholders can be %s or %b (text or binary)
    - return ``query`` (bytes), ``formats`` (list of formats) ``order``
      (sequence of names used in the query, in the position they appear, in

    """
    if not isinstance(query, bytes):
        # encoding from str already happened
        raise TypeError(
            f"the query should be str or bytes,"
            f" got {type(query).__name__} instead"
        )

    parts = split_query(query, codec.name)

    if isinstance(vars, Sequence) and not isinstance(vars, (bytes, str)):
        if len(vars) != len(parts) - 1:
            raise exc.ProgrammingError(
                f"the query has {len(parts) - 1} placeholders but"
                f" {len(vars)} parameters were passed"
            )
        if vars and not isinstance(parts[0][1], int):
            raise TypeError(
                "named placeholders require a mapping of parameters"
            )
        order = None

    elif isinstance(vars, Mapping):
        if vars and len(parts) > 1 and not isinstance(parts[0][1], bytes):
            raise TypeError(
                "positional placeholders (%s) require a sequence of parameters"
            )
        seen = {}
        order = []
        for part in parts[:-1]:
            name = codec.decode(part[1])[0]
            if name not in seen:
                n = len(seen)
                part[1] = n
                seen[name] = (n, part[2])
                order.append(name)
            else:
                if seen[name][1] != part[2]:
                    raise exc.ProgrammingError(
                        f"placeholder '{name}' cannot have different formats"
                    )
                part[1] = seen[name][0]

    else:
        raise TypeError(
            f"query parameters should be a sequence or a mapping,"
            f" got {type(vars).__name__}"
        )

    # Assemble query and parameters
    rv = []
    formats = []
    for part in parts[:-1]:
        rv.append(part[0])
        rv.append(b"$%d" % (part[1] + 1))
        formats.append(part[2])
    rv.append(parts[-1][0])

    return b"".join(rv), formats, order


_re_placeholder = re.compile(
    rb"""(?x)
        %                       # a literal %
        (?:
            (?:
                \( ([^)]+) \)   # or a name in (braces)
                .               # followed by a format
            )
            |
            (?:.)               # or any char, really
        )
        """
)


def split_query(query, encoding="ascii"):
    parts = []
    cur = 0

    # pairs [(fragment, match)], with the last match None
    m = None
    for m in _re_placeholder.finditer(query):
        pre = query[cur : m.span(0)[0]]
        parts.append([pre, m, None])
        cur = m.span(0)[1]
    if m is None:
        parts.append([query, None, None])
    else:
        parts.append([query[cur:], None, None])

    # drop the "%%", validate
    i = 0
    phtype = None
    while i < len(parts):
        part = parts[i]
        m = part[1]
        if m is None:
            break  # last part
        ph = m.group(0)
        if ph == b"%%":
            # unescape '%%' to '%' and merge the parts
            parts[i + 1][0] = part[0] + b"%" + parts[i + 1][0]
            del parts[i]
            continue
        if ph == b"%(":
            raise exc.ProgrammingError(
                f"incomplete placeholder:"
                f" '{query[m.span(0)[0]:].split()[0].decode(encoding)}'"
            )
        elif ph == b"% ":
            # explicit messasge for a typical error
            raise exc.ProgrammingError(
                "incomplete placeholder: '%'; if you want to use '%' as an"
                " operator you can double it up, i.e. use '%%'"
            )
        elif ph[-1:] not in b"bs":
            raise exc.ProgrammingError(
                f"only '%s' and '%b' placeholders allowed, got"
                f" {m.group(0).decode(encoding)}"
            )

        # Index or name
        if m.group(1) is None:
            part[1] = i
        else:
            part[1] = m.group(1)

        # Binary format
        part[2] = ph[-1:] == b"b"

        if phtype is None:
            phtype = type(part[1])
        else:
            if phtype is not type(part[1]):  # noqa
                raise exc.ProgrammingError(
                    "positional and named placeholders cannot be mixed"
                )

        i += 1

    return parts


def reorder_params(params, order):
    """
    Convert a mapping of parameters into an array in a specified order
    """
    try:
        return [params[item] for item in order]
    except KeyError:
        raise exc.ProgrammingError(
            f"query parameter missing:"
            f" {', '.join(sorted(i for i in order if i not in params))}"
        )