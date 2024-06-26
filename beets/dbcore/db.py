# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""The central Model and Database constructs for DBCore."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from abc import ABC
from collections import defaultdict
from sqlite3 import Connection, sqlite_version
from types import TracebackType
from typing import (
    Any,
    AnyStr,
    Callable,
    DefaultDict,
    Dict,
    Generator,
    Generic,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from packaging.version import Version
from rich import print
from rich_tables.generic import flexitable
from unidecode import unidecode

import beets

from ..util import cached_classproperty, functemplate
from . import types
from .query import FieldQuery, MatchQuery, NullSort, Query, Sort, TrueQuery

# convert data under 'json_str' type name to Python dictionary automatically
sqlite3.register_converter("json_str", json.loads)

DEBUG = bool(os.getenv("BEETS_DEBUG", False))


def print_query(sql, subvals=None):
    """If debugging, replace placeholders and print the query."""
    if not DEBUG:
        return
    topr = sql
    for val in subvals or []:
        topr = topr.replace("?", str(val), 1)
    print(flexitable({"sql": topr}), file=sys.stderr)


class DBAccessError(Exception):
    """The SQLite database became inaccessible.

    This can happen when trying to read or write the database when, for
    example, the database file is deleted or otherwise disappears. There
    is probably no way to recover from this error.
    """


class FormattedMapping(Mapping[str, str]):
    """A `dict`-like formatted view of a model.

    The accessor `mapping[key]` returns the formatted version of
    `model[key]` as a unicode string.

    The `included_keys` parameter allows filtering the fields that are
    returned. By default all fields are returned. Limiting to specific keys can
    avoid expensive per-item database queries.

    If `for_path` is true, all path separators in the formatted values
    are replaced.
    """

    ALL_KEYS = "*"

    def __init__(
        self,
        model: Model,
        included_keys: str = ALL_KEYS,
        for_path: bool = False,
    ):
        self.for_path = for_path
        self.model = model
        if included_keys == self.ALL_KEYS:
            # Performance note: this triggers a database query.
            self.model_keys = self.model.keys(True)
        else:
            self.model_keys = included_keys

    def __getitem__(self, key: str) -> str:
        if key in self.model_keys:
            return self._get_formatted(self.model, key)
        else:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.model_keys)

    def __len__(self) -> int:
        return len(self.model_keys)

    # The following signature is incompatible with `Mapping[str, str]`, since
    # the return type doesn't include `None` (but `default` can be `None`).
    def get(  # type: ignore
        self,
        key: str,
        default: Optional[str] = None,
    ) -> str:
        """Similar to Mapping.get(key, default), but always formats to str."""
        if default is None:
            default = self.model._type(key).format(None)
        return super().get(key, default)

    def _get_formatted(self, model: Model, key: str) -> str:
        value = model._type(key).format(model.get(key))
        if isinstance(value, bytes):
            value = value.decode("utf-8", "ignore")

        if self.for_path:
            sep_repl = cast(str, beets.config["path_sep_replace"].as_str())
            sep_drive = cast(str, beets.config["drive_sep_replace"].as_str())

            if re.match(r"^\w:", value):
                value = re.sub(r"(?<=^\w):", sep_drive, value)

            for sep in (os.path.sep, os.path.altsep):
                if sep:
                    value = value.replace(sep, sep_repl)

        return value


# NOTE: This seems like it should be a `Mapping`, i.e.
# ```
# class LazyConvertDict(Mapping[str, Any])
# ```
# but there are some conflicts with the `Mapping` protocol such that we
# can't do this without changing behaviour: In particular, iterators returned
# by some methods build intermediate lists, such that modification of the
# `LazyConvertDict` becomes safe during iteration. Some code does in fact rely
# on this.
class LazyConvertDict:
    """Lazily convert types for attributes fetched from the database"""

    def __init__(self, model_cls: "Model"):
        """Initialize the object empty"""
        # FIXME: Dict[str, SQLiteType]
        self._data: Dict[str, Any] = {}
        self.model_cls = model_cls
        self._converted: Dict[str, Any] = {}

    def init(self, data: Dict[str, Any]):
        """Set the base data that should be lazily converted"""
        self._data = data

    def _convert(self, key: str, value: Any):
        """Convert the attribute type according to the SQL type"""
        return self.model_cls._type(key).from_sql(value)

    def __setitem__(self, key: str, value: Any):
        """Set an attribute value, assume it's already converted"""
        self._converted[key] = value

    def __getitem__(self, key: str) -> Any:
        """Get an attribute value, converting the type on demand
        if needed
        """
        if key in self._converted:
            return self._converted[key]
        elif key in self._data:
            value = self._convert(key, self._data[key])
            self._converted[key] = value
            return value

    def __delitem__(self, key: str):
        """Delete both converted and base data"""
        if key in self._converted:
            del self._converted[key]
        if key in self._data:
            del self._data[key]

    def keys(self) -> List[str]:
        """Get a list of available field names for this object."""
        return list(self._converted.keys()) + list(self._data.keys())

    def copy(self) -> LazyConvertDict:
        """Create a copy of the object."""
        new = self.__class__(self.model_cls)
        new._data = self._data.copy()
        new._converted = self._converted.copy()
        return new

    # Act like a dictionary.

    def update(self, values: Mapping[str, Any]):
        """Assign all values in the given dict."""
        for key, value in values.items():
            self[key] = value

    def items(self) -> Iterable[Tuple[str, Any]]:
        """Iterate over (key, value) pairs that this object contains.
        Computed fields are not included.
        """
        for key in self:
            yield key, self[key]

    def get(self, key: str, default: Optional[Any] = None):
        """Get the value for a given key or `default` if it does not
        exist.
        """
        if key in self:
            return self[key]
        else:
            return default

    def __contains__(self, key: Any) -> bool:
        """Determine whether `key` is an attribute on this object."""
        return key in self._converted or key in self._data

    def __iter__(self) -> Iterator[str]:
        """Iterate over the available field names (excluding computed
        fields).
        """
        # NOTE: It would be nice to use the following:
        # yield from self._converted
        # yield from self._data
        # but that won't work since some code relies on modifying `self`
        # during iteration.
        return iter(self.keys())

    def __len__(self) -> int:
        # FIXME: This is incorrect due to duplication of keys
        return len(self._converted) + len(self._data)


# Abstract base for model classes.


class Model(ABC):
    """An abstract object representing an object in the database. Model
    objects act like dictionaries (i.e., they allow subscript access like
    ``obj['field']``). The same field set is available via attribute
    access as a shortcut (i.e., ``obj.field``). Three kinds of attributes are
    available:

    * **Fixed attributes** come from a predetermined list of field
      names. These fields correspond to SQLite table columns and are
      thus fast to read, write, and query.
    * **Flexible attributes** are free-form and do not need to be listed
      ahead of time.
    * **Computed attributes** are read-only fields computed by a getter
      function provided by a plugin.

    Access to all three field types is uniform: ``obj.field`` works the
    same regardless of whether ``field`` is fixed, flexible, or
    computed.

    Model objects can optionally be associated with a `Library` object,
    in which case they can be loaded and stored from the database. Dirty
    flags are used to track which fields need to be stored.
    """

    # Abstract components (to be provided by subclasses).

    _table: str
    """The main SQLite table name.
    """

    _flex_table: str
    """The flex field SQLite table name.
    """

    _fields: Dict[str, types.Type] = {}
    """A mapping indicating available "fixed" fields on this type. The
    keys are field names and the values are `Type` objects.
    """

    _search_fields: Sequence[str] = ()
    """The fields that should be queried by default by unqualified query
    terms.
    """

    _types: Dict[str, types.Type] = {}
    """Optional Types for non-fixed (i.e., flexible and computed) fields.
    """

    _sorts: Dict[str, Type[Sort]] = {}
    """Optional named sort criteria. The keys are strings and the values
    are subclasses of `Sort`.
    """

    _queries: Dict[str, Type[FieldQuery]] = {}
    """Named queries that use a field-like `name:value` syntax but which
    do not relate to any specific field.
    """

    _always_dirty = False
    """By default, fields only become "dirty" when their value actually
    changes. Enabling this flag marks fields as dirty even when the new
    value is the same as the old value (e.g., `o.f = o.f`).
    """

    _revision = -1
    """A revision number from when the model was loaded from or written
    to the database.
    """

    @cached_classproperty
    def _relation(cls) -> Type[Model]:
        """The model that this model is closely related to."""
        return cls

    @cached_classproperty
    def relation_join(cls) -> str:
        """Return the join required to include the related table in the query.

        This is intended to be used as a FROM clause in the SQL query.
        """
        return ""

    @cached_classproperty
    def table_with_flex_attrs(cls) -> str:
        """Return a SQL for entity table which includes aggregated flexible attributes.

        The clause selects entity rows, flexible attributes rows and LEFT JOINs
        them on entity id and 'entity_id' field respectively.

        'json_group_object' aggregate function groups flexible attributes into a
        single JSON object 'flex_attrs [json_str]'. The column name ending with
        ' [json_str]' means that this column is converted to a Python dictionary
        automatically (see 'register_converter' call at the top of this module).

        'REPLACE' function handles absence of flexible attributes and replaces
        some weird null JSON object (that SQLite gives us by default) with an
        empty JSON object.

        Availability of the 'flex_attrs' means we can query flexible attributes
        in the same manner we query other entity fields, see
        `FieldQuery.field`. This way, we also remove the need for an
        additional query to fetch them.

        Note: we use LEFT join to include entities without flexible attributes.
        Note: we name this SELECT clause after the original entity table name
        so that we can query it in the way like the original table.
        """
        flex_attrs = "REPLACE(json_group_object(key, value), '{:null}', '{}')"
        return f"""(
            SELECT
                *,
                {flex_attrs} AS "flex_attrs [json_str]"
            FROM {cls._table} LEFT JOIN (
                SELECT
                    entity_id,
                    key,
                    CAST(value AS text) AS value
                FROM {cls._flex_table}
            ) ON entity_id == {cls._table}.id
            GROUP BY {cls._table}.id
        ) {cls._table}
        """

    @cached_classproperty
    def all_model_db_fields(cls) -> Set[str]:
        return set()

    @classmethod
    def _getters(cls: Type["Model"]):
        """Return a mapping from field names to getter functions."""
        # We could cache this if it becomes a performance problem to
        # gather the getter mapping every time.
        raise NotImplementedError()

    def _template_funcs(self) -> Mapping[str, Callable[[str], str]]:
        """Return a mapping from function names to text-transformer
        functions.
        """
        # As above: we could consider caching this result.
        raise NotImplementedError()

    # Basic operation.

    def __init__(self, db: Optional[Database] = None, **values):
        """Create a new object with an optional Database association and
        initial field values.
        """
        self._db = db
        self._dirty: Set[str] = set()
        self._values_fixed = LazyConvertDict(self)
        self._values_flex = LazyConvertDict(self)

        # Initial contents.
        self.update(values)
        self.clear_dirty()

    @classmethod
    def _awaken(
        cls: Type[AnyModel],
        db: Optional[Database] = None,
        fixed_values: Dict[str, Any] = {},
        flex_values: Dict[str, Any] = {},
    ) -> AnyModel:
        """Create an object with values drawn from the database.

        This is a performance optimization: the checks involved with
        ordinary construction are bypassed.
        """
        obj = cls(db)

        obj._values_fixed.init(fixed_values)
        obj._values_flex.init(flex_values)

        return obj

    def __repr__(self) -> str:
        return "{}({})".format(
            type(self).__name__,
            ", ".join(f"{k}={v!r}" for k, v in dict(self).items()),
        )

    def clear_dirty(self):
        """Mark all fields as *clean* (i.e., not needing to be stored to
        the database). Also update the revision.
        """
        self._dirty = set()
        if self._db:
            self._revision = self._db.revision

    def _check_db(self, need_id: bool = True) -> Database:
        """Ensure that this object is associated with a database row: it
        has a reference to a database (`_db`) and an id. A ValueError
        exception is raised otherwise.
        """
        if not self._db:
            raise ValueError("{} has no database".format(type(self).__name__))
        if need_id and not self.id:
            raise ValueError("{} has no id".format(type(self).__name__))

        return self._db

    def copy(self) -> "Model":
        """Create a copy of the model object.

        The field values and other state is duplicated, but the new copy
        remains associated with the same database as the old object.
        (A simple `copy.deepcopy` will not work because it would try to
        duplicate the SQLite connection.)
        """
        new = self.__class__()
        new._db = self._db
        new._values_fixed = self._values_fixed.copy()
        new._values_flex = self._values_flex.copy()
        new._dirty = self._dirty.copy()
        return new

    # Essential field accessors.

    @classmethod
    def _type(cls, key) -> types.Type:
        """Get the type of a field, a `Type` instance.

        If the field has no explicit type, it is given the base `Type`,
        which does no conversion.
        """
        return cls._fields.get(key) or cls._types.get(key) or types.DEFAULT

    def _get(self, key, default: Any = None, raise_: bool = False):
        """Get the value for a field, or `default`. Alternatively,
        raise a KeyError if the field is not available.
        """
        getters = self._getters()
        if key in getters:  # Computed.
            return getters[key](self)
        elif key in self._fields:  # Fixed.
            if key in self._values_fixed:
                return self._values_fixed[key]
            else:
                return self._type(key).null
        elif key in self._values_flex:  # Flexible.
            return self._values_flex[key]
        elif raise_:
            raise KeyError(key)
        else:
            return default

    get = _get

    def __getitem__(self, key):
        """Get the value for a field. Raise a KeyError if the field is
        not available.
        """
        return self._get(key, raise_=True)

    def _setitem(self, key, value):
        """Assign the value for a field, return whether new and old value
        differ.
        """
        # Choose where to place the value.
        if key in self._fields:
            source = self._values_fixed
        else:
            source = self._values_flex

        # If the field has a type, filter the value.
        value = self._type(key).normalize(value)

        # Assign value and possibly mark as dirty.
        old_value = source.get(key)
        source[key] = value
        changed = old_value != value
        if self._always_dirty or changed:
            self._dirty.add(key)

        return changed

    def __setitem__(self, key, value):
        """Assign the value for a field."""
        self._setitem(key, value)

    def __delitem__(self, key):
        """Remove a flexible attribute from the model."""
        if key in self._values_flex:  # Flexible.
            del self._values_flex[key]
            self._dirty.add(key)  # Mark for dropping on store.
        elif key in self._fields:  # Fixed
            setattr(self, key, self._type(key).null)
        elif key in self._getters():  # Computed.
            raise KeyError(f"computed field {key} cannot be deleted")
        else:
            raise KeyError(f"no such field {key}")

    def keys(self, computed: bool = False):
        """Get a list of available field names for this object. The
        `computed` parameter controls whether computed (plugin-provided)
        fields are included in the key list.
        """
        base_keys = list(self._fields) + list(self._values_flex.keys())
        if computed:
            return base_keys + list(self._getters().keys())
        else:
            return base_keys

    @classmethod
    def all_keys(cls):
        """Get a list of available keys for objects of this type.
        Includes fixed and computed fields.
        """
        return list(cls._fields) + list(cls._getters().keys())

    # Act like a dictionary.

    def update(self, values):
        """Assign all values in the given dict."""
        for key, value in values.items():
            self[key] = value

    def items(self) -> Iterator[Tuple[str, Any]]:
        """Iterate over (key, value) pairs that this object contains.
        Computed fields are not included.
        """
        for key in self:
            yield key, self[key]

    def __contains__(self, key) -> bool:
        """Determine whether `key` is an attribute on this object."""
        return key in self.keys(computed=True)

    def __iter__(self) -> Iterator[str]:
        """Iterate over the available field names (excluding computed
        fields).
        """
        return iter(self.keys())

    # Convenient attribute access.

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(f"model has no attribute {key!r}")
        else:
            try:
                return self[key]
            except KeyError:
                raise AttributeError(f"no such field {key!r}")

    def __setattr__(self, key, value):
        if key.startswith("_"):
            super().__setattr__(key, value)
        else:
            self[key] = value

    def __delattr__(self, key):
        if key.startswith("_"):
            super().__delattr__(key)
        else:
            del self[key]

    # Database interaction (CRUD methods).

    def store(self, fields: Optional[Iterable[str]] = None):
        """Save the object's metadata into the library database.
        :param fields: the fields to be stored. If not specified, all fields
        will be.
        """
        if fields is None:
            fields = self._fields
        db = self._check_db()

        # Build assignments for query.
        assignments = []
        subvars = []
        for key in fields:
            if key != "id" and key in self._dirty:
                self._dirty.remove(key)
                assignments.append(key + "=?")
                value = self._type(key).to_sql(self[key])
                subvars.append(value)

        with db.transaction() as tx:
            # Main table update.
            if assignments:
                query = "UPDATE {} SET {} WHERE id=?".format(
                    self._table, ",".join(assignments)
                )
                subvars.append(self.id)
                tx.mutate(query, subvars)

            # Modified/added flexible attributes.
            for key, value in self._values_flex.items():
                if key in self._dirty:
                    self._dirty.remove(key)
                    tx.mutate(
                        "INSERT INTO {} "
                        "(entity_id, key, value) "
                        "VALUES (?, ?, ?);".format(self._flex_table),
                        (self.id, key, value),
                    )

            # Deleted flexible attributes.
            for key in self._dirty:
                tx.mutate(
                    f"DELETE FROM {self._flex_table} WHERE entity_id=? AND key=?",
                    (self.id, key),
                )

        self.clear_dirty()

    def load(self):
        """Refresh the object's metadata from the library database.

        If check_revision is true, the database is only queried loaded when a
        transaction has been committed since the item was last loaded.
        """
        db = self._check_db()
        if not self._dirty and db.revision == self._revision:
            # Exit early
            return
        stored_obj = db._get(type(self), self.id)
        assert stored_obj is not None, f"object {self.id} not in DB"
        self._values_fixed = LazyConvertDict(self)
        self._values_flex = LazyConvertDict(self)
        self.update(dict(stored_obj))
        self.clear_dirty()

    def remove(self):
        """Remove the object's associated rows from the database."""
        db = self._check_db()
        with db.transaction() as tx:
            tx.mutate(f"DELETE FROM {self._table} WHERE id=?", (self.id,))
            tx.mutate(
                f"DELETE FROM {self._flex_table} WHERE entity_id=?", (self.id,)
            )

    def add(self, db: Optional["Database"] = None):
        """Add the object to the library database. This object must be
        associated with a database; you can provide one via the `db`
        parameter or use the currently associated database.

        The object's `id` and `added` fields are set along with any
        current field values.
        """
        if db:
            self._db = db
        db = self._check_db(False)

        with db.transaction() as tx:
            new_id = tx.mutate(f"INSERT INTO {self._table} DEFAULT VALUES")
            self.id = new_id
            self.added = time.time()

            # Mark every non-null field as dirty and store.
            for key in self:
                if self[key] is not None:
                    self._dirty.add(key)
            self.store()

    # Formatting and templating.

    _formatter = FormattedMapping

    def formatted(
        self,
        included_keys: str = _formatter.ALL_KEYS,
        for_path: bool = False,
    ):
        """Get a mapping containing all values on this object formatted
        as human-readable unicode strings.
        """
        return self._formatter(self, included_keys, for_path)

    def evaluate_template(
        self,
        template: Union[str, functemplate.Template],
        for_path: bool = False,
    ) -> str:
        """Evaluate a template (a string or a `Template` object) using
        the object's fields. If `for_path` is true, then no new path
        separators will be added to the template.
        """
        # Perform substitution.
        if isinstance(template, str):
            t = functemplate.template(template)
        else:
            # Help out mypy
            t = template
        return t.substitute(
            self.formatted(for_path=for_path), self._template_funcs()
        )

    # Parsing.

    @classmethod
    def _parse(cls, key, string: str) -> Any:
        """Parse a string as a value for the given key."""
        if not isinstance(string, str):
            raise TypeError("_parse() argument must be a string")

        return cls._type(key).parse(string)

    def set_parse(self, key, string: str):
        """Set the object's key to a value represented by a string."""
        self[key] = self._parse(key, string)


# Database controller and supporting interfaces.


AnyModel = TypeVar("AnyModel", bound=Model)


class Results(Generic[AnyModel]):
    """An item query result set. Iterating over the collection lazily
    constructs Model objects that reflect database rows.
    """

    def __init__(
        self,
        model_class: Type[AnyModel],
        rows: List[Mapping],
        db: "Database",
        sort=None,
    ):
        """Create a result set that will construct objects of type
        `model_class`.

        `model_class` is a subclass of `Model` that will be
        constructed. `rows` is a query result: a list of mappings. The
        new objects will be associated with the database `db`.

        If `sort` is provided, it is used to sort the
        full list of results before returning. This means it is a "slow
        sort" and all objects must be built before returning the first
        one.
        """
        self.model_class = model_class
        self.rows = rows
        self.db = db
        self.sort = sort

        # We keep a queue of rows we haven't yet consumed for
        # materialization. We preserve the original total number of
        # rows.
        self._rows = rows
        self._row_count = len(rows)

        # The materialized objects corresponding to rows that have been
        # consumed.
        self._objects: List[AnyModel] = []

    def _get_objects(self) -> Iterator[AnyModel]:
        """Construct and generate Model objects for they query. The
        objects are returned in the order emitted from the database; no
        slow sort is applied.

        For performance, this generator caches materialized objects to
        avoid constructing them more than once. This way, iterating over
        a `Results` object a second time should be much faster than the
        first.
        """
        index = 0  # Position in the materialized objects.
        while index < len(self._objects) or self._rows:
            # Are there previously-materialized objects to produce?
            if index < len(self._objects):
                yield self._objects[index]
                index += 1

            # Otherwise, we consume another row, materialize its object
            # and produce it.
            else:
                while self._rows:
                    row = self._rows.pop(0)
                    obj = self._make_model(row)
                    self._objects.append(obj)
                    index += 1
                    yield obj
                    break

    def __iter__(self) -> Iterator[AnyModel]:
        """Construct and generate Model objects for all matching
        objects, in sorted order.
        """
        if self.sort:
            # Slow sort. Must build the full list first.
            objects = self.sort.sort(list(self._get_objects()))
            return iter(objects)

        else:
            # Objects are pre-sorted (i.e., by the database).
            return self._get_objects()

    def _make_model(self, row) -> AnyModel:
        """Create a Model object for the given row"""
        values = dict(row)
        flex_values = values.pop("flex_attrs") or {}

        # Construct the Python object
        obj = self.model_class._awaken(self.db, values, flex_values)
        return obj

    def __len__(self) -> int:
        """Get the number of matching objects."""
        if not self._rows:
            # Fully materialized. Just count the objects.
            return len(self._objects)
        else:
            # Just count the rows.
            return self._row_count

    def __nonzero__(self) -> bool:
        """Does this result contain any objects?"""
        return self.__bool__()

    def __bool__(self) -> bool:
        """Does this result contain any objects?"""
        return bool(len(self))

    def __getitem__(self, n):
        """Get the nth item in this result set. This is inefficient: all
        items up to n are materialized and thrown away.
        """
        if not self._rows and not self.sort:
            # Fully materialized and already in order. Just look up the
            # object.
            return self._objects[n]

        it = iter(self)
        try:
            for i in range(n):
                next(it)
            return next(it)
        except StopIteration:
            raise IndexError(f"result index {n} out of range")

    def get(self) -> Optional[AnyModel]:
        """Return the first matching object, or None if no objects
        match.
        """
        it = iter(self)
        try:
            return next(it)
        except StopIteration:
            return None


class Transaction:
    """A context manager for safe, concurrent access to the database.
    All SQL commands should be executed through a transaction.
    """

    _mutated = False
    """A flag storing whether a mutation has been executed in the
    current transaction.
    """

    def __init__(self, db: "Database"):
        self.db = db

    def __enter__(self) -> "Transaction":
        """Begin a transaction. This transaction may be created while
        another is active in a different thread.
        """
        with self.db._tx_stack() as stack:
            first = not stack
            stack.append(self)
        if first:
            # Beginning a "root" transaction, which corresponds to an
            # SQLite transaction.
            self.db._db_lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: Type[Exception],
        exc_value: Exception,
        traceback: TracebackType,
    ):
        """Complete a transaction. This must be the most recently
        entered but not yet exited transaction. If it is the last active
        transaction, the database updates are committed.
        """
        # Beware of races; currently secured by db._db_lock
        self.db.revision += self._mutated
        with self.db._tx_stack() as stack:
            assert stack.pop() is self
            empty = not stack
        if empty:
            # Ending a "root" transaction. End the SQLite transaction.
            self.db._connection().commit()
            self._mutated = False
            self.db._db_lock.release()

    def query(self, statement: str, subvals: Sequence = ()) -> List:
        """Execute an SQL statement with substitution values and return
        a list of rows from the database.
        """
        print_query(statement, subvals)
        cursor = self.db._connection().execute(statement, subvals)
        return cursor.fetchall()

    def mutate(self, statement: str, subvals: Sequence = ()) -> Any:
        """Execute an SQL statement with substitution values and return
        the row ID of the last affected row.
        """
        try:
            print_query(statement, subvals)
            cursor = self.db._connection().execute(statement, subvals)
        except sqlite3.OperationalError as e:
            # In two specific cases, SQLite reports an error while accessing
            # the underlying database file. We surface these exceptions as
            # DBAccessError so the application can abort.
            if e.args[0] in (
                "attempt to write a readonly database",
                "unable to open database file",
            ):
                raise DBAccessError(e.args[0])
            else:
                raise
        else:
            self._mutated = True
            return cursor.lastrowid

    def script(self, statements: str):
        """Execute a string containing multiple SQL statements."""
        # We don't know whether this mutates, but quite likely it does.
        self._mutated = True
        print_query(statements)
        self.db._connection().executescript(statements)


class Database:
    """A container for Model objects that wraps an SQLite database as
    the backend.
    """

    _models: Sequence[Type[Model]] = ()
    """The Model subclasses representing tables in this database.
    """

    supports_extensions = hasattr(sqlite3.Connection, "enable_load_extension")
    """Whether or not the current version of SQLite supports extensions"""

    revision = 0
    """The current revision of the database. To be increased whenever
    data is written in a transaction.
    """

    def __init__(self, path, timeout: float = 5.0):
        if sqlite3.threadsafety == 0:
            raise RuntimeError(
                "sqlite3 must be compiled with multi-threading support"
            )

        self.path = path
        self.timeout = timeout

        self._connections: Dict[int, sqlite3.Connection] = {}
        self._tx_stacks: DefaultDict[int, List[Transaction]] = defaultdict(list)
        self._extensions: List[str] = []

        # A lock to protect the _connections and _tx_stacks maps, which
        # both map thread IDs to private resources.
        self._shared_map_lock = threading.Lock()

        # A lock to protect access to the database itself. SQLite does
        # allow multiple threads to access the database at the same
        # time, but many users were experiencing crashes related to this
        # capability: where SQLite was compiled without HAVE_USLEEP, its
        # backoff algorithm in the case of contention was causing
        # whole-second sleeps (!) that would trigger its internal
        # timeout. Using this lock ensures only one SQLite transaction
        # is active at a time.
        self._db_lock = threading.Lock()

        # Set up database schema.
        for model_cls in self._models:
            self._make_table(model_cls._table, model_cls._fields)
            self._make_attribute_table(model_cls._flex_table)

    # Primitive access control: connections and transactions.

    def _connection(self) -> Connection:
        """Get a SQLite connection object to the underlying database.
        One connection object is created per thread.
        """
        thread_id = threading.current_thread().ident
        # Help the type checker: ident can only be None if the thread has not
        # been started yet; but since this results from current_thread(), that
        # can't happen
        assert thread_id is not None

        with self._shared_map_lock:
            if thread_id in self._connections:
                return self._connections[thread_id]
            else:
                conn = self._create_connection()
                self._connections[thread_id] = conn
                return conn

    def _create_connection(self) -> Connection:
        """Create a SQLite connection to the underlying database.

        Makes a new connection every time. If you need to configure the
        connection settings (e.g., add custom functions), override this
        method.
        """
        # Make a new connection. The `sqlite3` module can't use
        # bytestring paths here on Python 3, so we need to
        # provide a `str` using `os.fsdecode`.
        conn = sqlite3.connect(
            os.fsdecode(self.path),
            timeout=self.timeout,
            # We have our own same-thread checks in _connection(), but need to
            # call conn.close() in _close()
            check_same_thread=False,
            # enable type name "col [type]" conversion (`register_converter`)
            detect_types=sqlite3.PARSE_COLNAMES,
        )
        self.add_functions(conn)

        if self.supports_extensions:
            conn.enable_load_extension(True)

            # Load any extension that are already loaded for other connections.
            for path in self._extensions:
                conn.load_extension(path)

        # Access SELECT results like dictionaries.
        conn.row_factory = sqlite3.Row
        return conn

    def add_functions(self, conn):
        def regexp(value, pattern):
            if isinstance(value, bytes):
                value = value.decode()
            return (
                value is not None and re.search(pattern, str(value)) is not None
            )

        def bytelower(bytestring: Optional[AnyStr]) -> Optional[AnyStr]:
            """A custom ``bytelower`` sqlite function so we can compare
            bytestrings in a semi case insensitive fashion.

            This is to work around sqlite builds are that compiled with
            ``-DSQLITE_LIKE_DOESNT_MATCH_BLOBS``. See
            ``https://github.com/beetbox/beets/issues/2172`` for details.
            """
            if bytestring is not None:
                return bytestring.lower()

            return bytestring

        def json_patch(first: str, second: str) -> str:
            """Implementation of the 'json_patch' SQL function.

            This function merges two JSON strings together.
            """
            first_dict = json.loads(first)
            second_dict = json.loads(second)
            first_dict.update(second_dict)
            return json.dumps(first_dict)

        def json_extract(json_str: str, key: str) -> Optional[str]:
            """Simple implementation of the 'json_extract' SQLite function.

            The original implementation in SQLite allows traversing objects of
            any depth. Here, we only ever deal with a flat dictionary, thus
            we can simplify the implementation to a single 'get' call.
            """
            if json_str:
                return json.loads(json_str).get(key.replace("$.", ""))

            return None

        class JSONGroupObject:
            """Implementation of the 'json_group_object' SQLite aggregate.

            An aggregate function which accepts two values (key, val) and
            groups all {key: val} pairs into a single object.

            It is found in the json1 extension which is included in SQLite
            by default since version 3.38.0 (2022-02-22). To ensure support
            for older SQLite versions, we add our implementation.

            Notably, it does not exist on Windows in Python 3.8.

            Consider the following table

            id  key    val
            1   plays  "10"
            1   skips  "20"
            2   city   "London"

            SELECT id, group_to_json(key, val) GROUP BY id
                1, '{"plays": "10", "skips": "20"}'
                2, '{"city": "London"}'
            """

            def __init__(self):
                self.flex = {}

            def step(self, field, value):
                if field:
                    self.flex[field] = value

            def finalize(self):
                return json.dumps(self.flex)

        conn.create_function("regexp", 2, regexp)
        conn.create_function("unidecode", 1, unidecode)
        conn.create_function("bytelower", 1, bytelower)
        if Version(sqlite_version) < Version("3.38.0"):
            # create 'json_group_object' for older SQLite versions that do
            # not include the json1 extension by default
            conn.create_aggregate("json_group_object", 2, JSONGroupObject)
            conn.create_function("json_patch", 2, json_patch)
            conn.create_function("json_extract", 2, json_extract)

    def _close(self):
        """Close the all connections to the underlying SQLite database
        from all threads. This does not render the database object
        unusable; new connections can still be opened on demand.
        """
        with self._shared_map_lock:
            while self._connections:
                _thread_id, conn = self._connections.popitem()
                conn.close()

    @contextlib.contextmanager
    def _tx_stack(self) -> Generator[List, None, None]:
        """A context manager providing access to the current thread's
        transaction stack. The context manager synchronizes access to
        the stack map. Transactions should never migrate across threads.
        """
        thread_id = threading.current_thread().ident
        # Help the type checker: ident can only be None if the thread has not
        # been started yet; but since this results from current_thread(), that
        # can't happen
        assert thread_id is not None

        with self._shared_map_lock:
            yield self._tx_stacks[thread_id]

    def transaction(self) -> Transaction:
        """Get a :class:`Transaction` object for interacting directly
        with the underlying SQLite database.
        """
        return Transaction(self)

    def load_extension(self, path: str):
        """Load an SQLite extension into all open connections."""
        if not self.supports_extensions:
            raise ValueError(
                "this sqlite3 installation does not support extensions"
            )

        self._extensions.append(path)

        # Load the extension into every open connection.
        for conn in self._connections.values():
            conn.load_extension(path)

    # Schema setup and migration.

    def _make_table(self, table: str, fields: Mapping[str, types.Type]):
        """Set up the schema of the database. `fields` is a mapping
        from field names to `Type`s. Columns are added if necessary.
        """
        # Get current schema.
        with self.transaction() as tx:
            rows = tx.query("PRAGMA table_info(%s)" % table)
        current_fields = {row[1] for row in rows}

        field_names = set(fields.keys())
        if current_fields.issuperset(field_names):
            # Table exists and has all the required columns.
            return

        if not current_fields:
            # No table exists.
            columns = []
            for name, typ in fields.items():
                columns.append(f"{name} {typ.sql}")
            setup_sql = "CREATE TABLE {} ({});\n".format(
                table, ", ".join(columns)
            )

        else:
            # Table exists does not match the field set.
            setup_sql = ""
            for name, typ in fields.items():
                if name in current_fields:
                    continue
                setup_sql += "ALTER TABLE {} ADD COLUMN {} {};\n".format(
                    table, name, typ.sql
                )

        with self.transaction() as tx:
            tx.script(setup_sql)

    def _make_attribute_table(self, flex_table: str):
        """Create a table and associated index for flexible attributes
        for the given entity (if they don't exist).
        """
        with self.transaction() as tx:
            tx.script(
                """
                CREATE TABLE IF NOT EXISTS {0} (
                    id INTEGER PRIMARY KEY,
                    entity_id INTEGER,
                    key TEXT,
                    value TEXT,
                    UNIQUE(entity_id, key) ON CONFLICT REPLACE);
                CREATE INDEX IF NOT EXISTS {0}_by_entity
                    ON {0} (entity_id);
                """.format(
                    flex_table
                )
            )

    # Querying.

    def _fetch(
        self,
        model_cls: Type[AnyModel],
        query: Optional[Query] = None,
        sort: Optional[Sort] = None,
    ) -> Results[AnyModel]:
        """Fetch the objects of type `model_cls` matching the given
        query. The query may be given as a string, string sequence, a
        Query object, or None (to fetch everything). `sort` is an
        `Sort` object.
        """
        query = query or TrueQuery()  # A null query.
        sort = sort or NullSort()  # Unsorted.
        where, subvals = query.clause()
        order_by = sort.order_clause()

        this_table = model_cls._table
        select_fields = [f"{this_table}.*"]
        _from = model_cls.table_with_flex_attrs

        required_fields = query.field_names
        if required_fields - model_cls._fields.keys():
            _from += f" {model_cls.relation_join}"

            if required_fields - model_cls.all_model_db_fields:
                # merge all flexible attribute into a single JSON field
                select_fields.append(
                    f"""
                    json_patch(
                        COALESCE({this_table}."flex_attrs [json_str]", '{{}}'),
                        COALESCE({model_cls._relation._table}."flex_attrs [json_str]", '{{}}')
                    ) AS all_flex_attrs
                    """  # noqa: E501
                )

        sql = f"SELECT {', '.join(select_fields)} FROM {_from} WHERE {where or 1} GROUP BY {this_table}.id"  # noqa: E501

        if order_by:
            # the sort field may exist in both 'items' and 'albums' tables
            # (when they are joined), causing ambiguous column OperationalError
            # if we try to order directly.
            # Since the join is required only for filtering, we can filter in
            # a subquery and order the result, which returns unique fields.
            sql = f"SELECT * FROM ({sql}) ORDER BY {order_by}"

        with self.transaction() as tx:
            rows = tx.query(sql, subvals)

        return Results(
            model_cls,
            rows,
            self,
            sort if sort.is_slow() else None,  # Slow sort component.
        )

    def _get(
        self,
        model_cls: Type[AnyModel],
        id,
    ) -> Optional[AnyModel]:
        """Get a Model object by its id or None if the id does not
        exist.
        """
        return self._fetch(model_cls, MatchQuery("id", id)).get()
