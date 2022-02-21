""" Helper class for managing database used by PhotoExporter for tracking state of exports and updates """


import datetime
import json
import logging
import os
import pathlib
import sqlite3
import sys
from io import StringIO
from sqlite3 import Error
from tempfile import TemporaryDirectory
from typing import Optional, Tuple, Union

from ._constants import OSXPHOTOS_EXPORT_DB
from ._version import __version__
from .fileutil import FileUtil
from .utils import normalize_fs_path

__all__ = [
    "ExportDB",
    "ExportDBInMemory",
    "ExportDBTemp",
]

OSXPHOTOS_EXPORTDB_VERSION = "6.0"
OSXPHOTOS_ABOUT_STRING = f"Created by osxphotos version {__version__} (https://github.com/RhetTbull/osxphotos) on {datetime.datetime.now()}"


class ExportDB:
    """Interface to sqlite3 database used to store state information for osxphotos export command"""

    def __init__(self, dbfile, export_dir):
        """create a new ExportDB object

        Args:
            dbfile: path to osxphotos export database file
            export_dir: path to directory where exported files are stored
            memory: if True, use in-memory database
        """

        self._dbfile = dbfile
        # export_dir is required as all files referenced by get_/set_uuid_for_file will be converted to
        # relative paths to this path
        # this allows the entire export tree to be moved to a new disk/location
        # whilst preserving the UUID to filename mapping
        self._path = export_dir
        self._conn = self._open_export_db(dbfile)
        self._perform_db_maintenace(self._conn)
        self._insert_run_info()

    def get_file_record(self, filename: Union[pathlib.Path, str]) -> "ExportRecord":
        """get info for filename and uuid

        Returns: an ExportRecord object
        """
        filename = self._relative_filepath(filename)
        filename_normalized = self._normalize_filepath(filename)
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT uuid FROM export_data WHERE filepath_normalized = ?;",
            (filename_normalized,),
        ).fetchone()

        if not row:
            return None
        return ExportRecord(conn, filename_normalized)

    def create_file_record(
        self, filename: Union[pathlib.Path, str], uuid: str
    ) -> "ExportRecord":
        """create a new record for filename and uuid

        Returns: an ExportRecord object
        """
        filename = self._relative_filepath(filename)
        filename_normalized = self._normalize_filepath(filename)
        conn = self._conn
        c = conn.cursor()
        c.execute(
            "INSERT INTO export_data (filepath, filepath_normalized, uuid) VALUES (?, ?, ?);",
            (filename, filename_normalized, uuid),
        )
        conn.commit()
        return ExportRecord(conn, filename_normalized)

    def create_or_get_file_record(
        self, filename: Union[pathlib.Path, str], uuid: str
    ) -> "ExportRecord":
        """create a new record for filename and uuid or return existing record

        Returns: an ExportRecord object
        """
        filename = self._relative_filepath(filename)
        filename_normalized = self._normalize_filepath(filename)
        conn = self._conn
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO export_data (filepath, filepath_normalized, uuid) VALUES (?, ?, ?);",
            (filename, filename_normalized, uuid),
        )
        conn.commit()
        return ExportRecord(conn, filename_normalized)

    def get_uuid_for_file(self, filename):
        """query database for filename and return UUID
        returns None if filename not found in database
        """
        filepath_normalized = self._normalize_filepath_relative(filename)
        conn = self._conn
        try:
            c = conn.cursor()
            c.execute(
                "SELECT uuid FROM export_data WHERE filepath_normalized = ?",
                (filepath_normalized,),
            )
            results = c.fetchone()
            uuid = results[0] if results else None
        except Error as e:
            logging.warning(e)
            uuid = None
        return uuid

    def get_photoinfo_for_uuid(self, uuid):
        """returns the photoinfo JSON struct for a UUID"""
        conn = self._conn
        try:
            c = conn.cursor()
            c.execute("SELECT photoinfo FROM photoinfo WHERE uuid = ?", (uuid,))
            results = c.fetchone()
            info = results[0] if results else None
        except Error as e:
            logging.warning(e)
            info = None

        return info

    def set_photoinfo_for_uuid(self, uuid, info):
        """sets the photoinfo JSON struct for a UUID"""
        conn = self._conn
        try:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO photoinfo(uuid, photoinfo) VALUES (?, ?);",
                (uuid, info),
            )
            conn.commit()
        except Error as e:
            logging.warning(e)

    def get_previous_uuids(self):
        """returns list of UUIDs of previously exported photos found in export database"""
        conn = self._conn
        previous_uuids = []
        try:
            c = conn.cursor()
            c.execute("SELECT DISTINCT uuid FROM export_data")
            results = c.fetchall()
            previous_uuids = [row[0] for row in results]
        except Error as e:
            logging.warning(e)
        return previous_uuids

    def set_config(self, config_data):
        """set config in the database"""
        conn = self._conn
        try:
            dt = datetime.datetime.now().isoformat()
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO config(datetime, config) VALUES (?, ?);",
                (dt, config_data),
            )
            conn.commit()
        except Error as e:
            logging.warning(e)

    def close(self):
        """close the database connection"""
        try:
            self._conn.close()
        except Error as e:
            logging.warning(e)

    def _open_export_db(self, dbfile):
        """open export database and return a db connection
        if dbfile does not exist, will create and initialize the database
        if dbfile needs to be upgraded, will perform needed migrations
        returns: connection to the database
        """

        if not os.path.isfile(dbfile):
            conn = self._get_db_connection(dbfile)
            if not conn:
                raise Exception("Error getting connection to database {dbfile}")
            self._create_or_migrate_db_tables(conn)
            self.was_created = True
            self.was_upgraded = ()
        else:
            conn = self._get_db_connection(dbfile)
            self.was_created = False
            version_info = self._get_database_version(conn)
            if version_info[1] < OSXPHOTOS_EXPORTDB_VERSION:
                self._create_or_migrate_db_tables(conn)
                self.was_upgraded = (version_info[1], OSXPHOTOS_EXPORTDB_VERSION)
            else:
                self.was_upgraded = ()
        self.version = OSXPHOTOS_EXPORTDB_VERSION

        # turn on performance optimizations
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute("PRAGMA cache_size=-100000;")
        c.execute("PRAGMA temp_store=MEMORY;")

        return conn

    def _get_db_connection(self, dbfile):
        """return db connection to dbname"""
        try:
            conn = sqlite3.connect(dbfile)
        except Error as e:
            logging.warning(e)
            conn = None

        return conn

    def _get_database_version(self, conn):
        """return tuple of (osxphotos, exportdb) versions for database connection conn"""
        version_info = conn.execute(
            "SELECT osxphotos, exportdb, max(id) FROM version"
        ).fetchone()
        return (version_info[0], version_info[1])

    def _create_or_migrate_db_tables(self, conn):
        """create (if not already created) the necessary db tables for the export database and apply any needed migrations

        Args:
            conn: sqlite3 db connection
        """
        try:
            version = self._get_database_version(conn)
        except Exception as e:
            version = (__version__, "4.3")

        # Current for version 4.3, for anything greater, do a migration after creation
        sql_commands = [
            """ CREATE TABLE IF NOT EXISTS version (
                    id INTEGER PRIMARY KEY,
                    osxphotos TEXT,
                    exportdb TEXT 
                    ); """,
            """ CREATE TABLE IF NOT EXISTS about (
                    id INTEGER PRIMARY KEY,
                    about TEXT
                    );""",
            """ CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY,
                    filepath TEXT NOT NULL,
                    filepath_normalized TEXT NOT NULL,
                    uuid TEXT,
                    orig_mode INTEGER,
                    orig_size INTEGER,
                    orig_mtime REAL,
                    exif_mode INTEGER,
                    exif_size INTEGER,
                    exif_mtime REAL
                    ); """,
            """ CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    datetime TEXT,
                    python_path TEXT,
                    script_name TEXT,
                    args TEXT,
                    cwd TEXT 
                    ); """,
            """ CREATE TABLE IF NOT EXISTS info (
                    id INTEGER PRIMARY KEY,
                    uuid text NOT NULL,
                    json_info JSON 
                    ); """,
            """ CREATE TABLE IF NOT EXISTS exifdata (
                    id INTEGER PRIMARY KEY,
                    filepath_normalized TEXT NOT NULL,
                    json_exifdata JSON 
                    ); """,
            """ CREATE TABLE IF NOT EXISTS edited (
                    id INTEGER PRIMARY KEY,
                    filepath_normalized TEXT NOT NULL,
                    mode INTEGER,
                    size INTEGER,
                    mtime REAL
                    ); """,
            """ CREATE TABLE IF NOT EXISTS converted (
                    id INTEGER PRIMARY KEY,
                    filepath_normalized TEXT NOT NULL,
                    mode INTEGER,
                    size INTEGER,
                    mtime REAL
                    ); """,
            """ CREATE TABLE IF NOT EXISTS sidecar (
                    id INTEGER PRIMARY KEY,
                    filepath_normalized TEXT NOT NULL,
                    sidecar_data TEXT,
                    mode INTEGER,
                    size INTEGER,
                    mtime REAL
                    ); """,
            """ CREATE TABLE IF NOT EXISTS detected_text (
                    id INTEGER PRIMARY KEY,
                    uuid TEXT NOT NULL,
                    text_data JSON
                    ); """,
            """ CREATE UNIQUE INDEX IF NOT EXISTS idx_files_filepath_normalized on files (filepath_normalized); """,
            """ CREATE UNIQUE INDEX IF NOT EXISTS idx_info_uuid on info (uuid); """,
            """ CREATE UNIQUE INDEX IF NOT EXISTS idx_exifdata_filename on exifdata (filepath_normalized); """,
            """ CREATE UNIQUE INDEX IF NOT EXISTS idx_edited_filename on edited (filepath_normalized);""",
            """ CREATE UNIQUE INDEX IF NOT EXISTS idx_converted_filename on converted (filepath_normalized);""",
            """ CREATE UNIQUE INDEX IF NOT EXISTS idx_sidecar_filename on sidecar (filepath_normalized);""",
            """ CREATE UNIQUE INDEX IF NOT EXISTS idx_detected_text on detected_text (uuid);""",
        ]
        # create the tables if needed
        try:
            c = conn.cursor()
            for cmd in sql_commands:
                c.execute(cmd)
            c.execute(
                "INSERT INTO version(osxphotos, exportdb) VALUES (?, ?);",
                (__version__, OSXPHOTOS_EXPORTDB_VERSION),
            )
            c.execute("INSERT INTO about(about) VALUES (?);", (OSXPHOTOS_ABOUT_STRING,))
            conn.commit()
        except Error as e:
            logging.warning(e)

        # perform needed migrations
        if version[1] < "4.3":
            self._migrate_normalized_filepath(conn)

        if version[1] < "5.0":
            self._migrate_4_3_to_5_0(conn)

        if version[1] < "6.0":
            # create export_data table
            self._migrate_5_0_to_6_0(conn)

        conn.execute("VACUUM;")
        conn.commit()

    def __del__(self):
        """ensure the database connection is closed"""
        try:
            self._conn.close()
        except:
            pass

    def _insert_run_info(self):
        dt = datetime.datetime.utcnow().isoformat()
        python_path = sys.executable
        cmd = sys.argv[0]
        args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
        cwd = os.getcwd()
        conn = self._conn
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO runs (datetime, python_path, script_name, args, cwd) VALUES (?, ?, ?, ?, ?)",
                (dt, python_path, cmd, args, cwd),
            )

            conn.commit()
        except Error as e:
            logging.warning(e)

    def _relative_filepath(self, filepath: Union[str, pathlib.Path]) -> str:
        """return filepath relative to self._path"""
        return str(pathlib.Path(filepath).relative_to(self._path))

    def _normalize_filepath(self, filepath: Union[str, pathlib.Path]) -> str:
        """normalize filepath for unicode, lower case"""
        return normalize_fs_path(str(filepath)).lower()

    def _normalize_filepath_relative(self, filepath: Union[str, pathlib.Path]) -> str:
        """normalize filepath for unicode, relative path (to export dir), lower case"""
        filepath = self._relative_filepath(filepath)
        return normalize_fs_path(str(filepath)).lower()

    def _migrate_normalized_filepath(self, conn):
        """Fix all filepath_normalized columns for unicode normalization"""
        # Prior to database version 4.3, filepath_normalized was not normalized for unicode
        c = conn.cursor()
        migration_sql = [
            """ CREATE TABLE IF NOT EXISTS files_migrate (
                    id INTEGER PRIMARY KEY,
                    filepath TEXT NOT NULL,
                    filepath_normalized TEXT NOT NULL,
                    uuid TEXT,
                    orig_mode INTEGER,
                    orig_size INTEGER,
                    orig_mtime REAL,
                    exif_mode INTEGER,
                    exif_size INTEGER,
                    exif_mtime REAL,
                    UNIQUE(filepath_normalized)
                    ); """,
            """ INSERT INTO files_migrate SELECT * FROM files;""",
            """ DROP TABLE files;""",
            """ ALTER TABLE files_migrate RENAME TO files;""",
        ]
        for sql in migration_sql:
            c.execute(sql)
        conn.commit()

        for table in ["converted", "edited", "exifdata", "files", "sidecar"]:
            old_values = c.execute(
                f"SELECT filepath_normalized, id FROM {table}"
            ).fetchall()
            new_values = [
                (self._normalize_filepath(filepath_normalized), id_)
                for filepath_normalized, id_ in old_values
            ]
            c.executemany(
                f"UPDATE {table} SET filepath_normalized=? WHERE id=?", new_values
            )
        conn.commit()

    def _migrate_4_3_to_5_0(self, conn):
        """Migrate database from version 4.3 to 5.0"""
        try:
            c = conn.cursor()
            # add metadata column to files to support --force-update
            c.execute("ALTER TABLE files ADD COLUMN metadata TEXT;")
            conn.commit()
        except Error as e:
            logging.warning(e)

    def _migrate_5_0_to_6_0(self, conn):
        try:
            c = conn.cursor()

            # add export_data table
            c.execute(
                """ CREATE TABLE IF NOT EXISTS export_data(
                        id INTEGER PRIMARY KEY,
                        filepath_normalized TEXT NOT NULL,
                        filepath TEXT NOT NULL,
                        uuid TEXT NOT NULL,
                        src_mode INTEGER,
                        src_size INTEGER,
                        src_mtime REAL,
                        dest_mode INTEGER,
                        dest_size INTEGER,
                        dest_mtime REAL,
                        digest TEXT,
                        exifdata JSON,
                        export_options INTEGER,
                        UNIQUE(filepath_normalized)
                    ); """,
            )
            c.execute(
                """ CREATE UNIQUE INDEX IF NOT EXISTS idx_export_data_filepath_normalized on export_data (filepath_normalized); """,
            )

            # migrate data
            c.execute(
                """ INSERT INTO export_data (filepath_normalized, filepath, uuid) SELECT filepath_normalized, filepath, uuid FROM files;""",
            )
            c.execute(
                """ UPDATE export_data 
                    SET (src_mode, src_size, src_mtime) = 
                    (SELECT mode, size, mtime 
                    FROM edited 
                    WHERE export_data.filepath_normalized = edited.filepath_normalized);
               """,
            )
            c.execute(
                """ UPDATE export_data 
                    SET (dest_mode, dest_size, dest_mtime) = 
                    (SELECT orig_mode, orig_size, orig_mtime 
                    FROM files 
                    WHERE export_data.filepath_normalized = files.filepath_normalized);
               """,
            )
            c.execute(
                """ UPDATE export_data SET digest = 
                          (SELECT metadata FROM files 
                          WHERE files.filepath_normalized = export_data.filepath_normalized
                          ); """
            )
            c.execute(
                """ UPDATE export_data SET exifdata = 
                          (SELECT json_exifdata FROM exifdata 
                          WHERE exifdata.filepath_normalized = export_data.filepath_normalized
                          ); """
            )

            # create config table
            c.execute(
                """ CREATE TABLE IF NOT EXISTS config (
                        id INTEGER PRIMARY KEY,
                        datetime TEXT,
                        config TEXT 
                ); """
            )

            # create photoinfo table
            c.execute(
                """ CREATE TABLE IF NOT EXISTS photoinfo (
                        id INTEGER PRIMARY KEY,
                        uuid TEXT NOT NULL,
                        photoinfo JSON,
                        UNIQUE(uuid)
                ); """
            )
            c.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_photoinfo_uuid on photoinfo (uuid);"""
            )
            c.execute(
                """ INSERT INTO photoinfo (uuid, photoinfo) SELECT uuid, json_info FROM info;"""
            )

            # drop indexes no longer needed
            c.execute("DROP INDEX IF EXISTS idx_files_filepath_normalized;")
            c.execute("DROP INDEX IF EXISTS idx_exifdata_filename;")
            c.execute("DROP INDEX IF EXISTS idx_edited_filename;")
            c.execute("DROP INDEX IF EXISTS idx_converted_filename;")
            c.execute("DROP INDEX IF EXISTS idx_sidecar_filename;")
            c.execute("DROP INDEX IF EXISTS idx_detected_text;")

            # drop tables no longer needed
            c.execute("DROP TABLE IF EXISTS files;")
            c.execute("DROP TABLE IF EXISTS info;")
            c.execute("DROP TABLE IF EXISTS exifdata;")
            c.execute("DROP TABLE IF EXISTS edited;")
            c.execute("DROP TABLE IF EXISTS converted;")
            c.execute("DROP TABLE IF EXISTS sidecar;")
            c.execute("DROP TABLE IF EXISTS detected_text;")

            conn.commit()
        except Error as e:
            logging.warning(e)

    def _perform_db_maintenace(self, conn):
        """Perform database maintenance"""
        try:
            c = conn.cursor()
            c.execute(
                """DELETE FROM config
                    WHERE id < (
                        SELECT MIN(id)
                        FROM (SELECT id FROM config ORDER BY id DESC LIMIT 9)
                    );
                """
            )
            conn.commit()
        except Error as e:
            logging.warning(e)


class ExportDBInMemory(ExportDB):
    """In memory version of ExportDB
    Copies the on-disk database into memory so it may be operated on without
    modifying the on-disk version
    """

    def __init__(self, dbfile, export_dir):
        self._dbfile = dbfile or f"./{OSXPHOTOS_EXPORT_DB}"
        # export_dir is required as all files referenced by get_/set_uuid_for_file will be converted to
        # relative paths to this path
        # this allows the entire export tree to be moved to a new disk/location
        # whilst preserving the UUID to filename mapping
        self._path = export_dir
        self._conn = self._open_export_db(self._dbfile)
        self._insert_run_info()

    def _open_export_db(self, dbfile):
        """open export database and return a db connection
        returns: connection to the database
        """
        if not os.path.isfile(dbfile):
            conn = self._get_db_connection()
            if not conn:
                raise Exception("Error getting connection to in-memory database")
            self._create_or_migrate_db_tables(conn)
            self.was_created = True
            self.was_upgraded = ()
        else:
            try:
                conn = sqlite3.connect(dbfile)
            except Error as e:
                logging.warning(e)
                raise e from e

            tempfile = StringIO()
            for line in conn.iterdump():
                tempfile.write("%s\n" % line)
            conn.close()
            tempfile.seek(0)

            # Create a database in memory and import from tempfile
            conn = sqlite3.connect(":memory:")
            conn.cursor().executescript(tempfile.read())
            conn.commit()
            self.was_created = False
            version_info = self._get_database_version(conn)
            if version_info[1] < OSXPHOTOS_EXPORTDB_VERSION:
                self._create_or_migrate_db_tables(conn)
                self.was_upgraded = (version_info[1], OSXPHOTOS_EXPORTDB_VERSION)
            else:
                self.was_upgraded = ()
        self.version = OSXPHOTOS_EXPORTDB_VERSION

        return conn

    def _get_db_connection(self):
        """return db connection to in memory database"""
        try:
            conn = sqlite3.connect(":memory:")
        except Error as e:
            logging.warning(e)
            conn = None

        return conn


class ExportDBTemp(ExportDBInMemory):
    """Temporary in-memory version of ExportDB"""

    def __init__(self):
        self._temp_dir = TemporaryDirectory()
        self._dbfile = f"{self._temp_dir.name}/{OSXPHOTOS_EXPORT_DB}"
        self._path = self._temp_dir.name
        self._conn = self._open_export_db(self._dbfile)
        self._insert_run_info()

    def _relative_filepath(self, filepath: Union[str, pathlib.Path]) -> str:
        """Overrides _relative_filepath to return a path for use in the temp db"""
        filepath = str(filepath)
        if filepath[0] == "/":
            return filepath[1:]
        return filepath


class ExportRecord:
    """ExportRecord class"""

    __slots__ = [
        "_conn",
        "_context_manager",
        "_filepath_normalized",
    ]

    def __init__(self, conn, filepath_normalized):
        self._conn = conn
        self._filepath_normalized = filepath_normalized
        self._context_manager = False

    @property
    def filepath(self):
        """return filepath"""
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT filepath FROM export_data WHERE filepath_normalized = ?;",
            (self._filepath_normalized,),
        ).fetchone()
        if row:
            return row[0]

        raise ValueError(
            f"No filepath found in database for {self._filepath_normalized}"
        )

    @property
    def filepath_normalized(self):
        """return filepath_normalized"""
        return self._filepath_normalized

    @property
    def uuid(self):
        """return uuid"""
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT uuid FROM export_data WHERE filepath_normalized = ?;",
            (self._filepath_normalized,),
        ).fetchone()
        if row:
            return row[0]

        raise ValueError(f"No uuid found in database for {self._filepath_normalized}")

    @property
    def digest(self):
        """returns the digest value"""
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT digest FROM export_data WHERE filepath_normalized = ?;",
            (self._filepath_normalized,),
        ).fetchone()
        if row:
            return row[0]

        raise ValueError(f"No digest found in database for {self._filepath_normalized}")

    @digest.setter
    def digest(self, value):
        """set digest value"""
        conn = self._conn
        c = conn.cursor()
        c.execute(
            "UPDATE export_data SET digest = ? WHERE filepath_normalized = ?;",
            (value, self._filepath_normalized),
        )
        if not self._context_manager:
            conn.commit()

    @property
    def exifdata(self):
        """returns exifdata value for record"""
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT exifdata FROM export_data WHERE filepath_normalized = ?;",
            (self._filepath_normalized,),
        ).fetchone()
        if row:
            return row[0]

        raise ValueError(
            f"No exifdata found in database for {self._filepath_normalized}"
        )

    @exifdata.setter
    def exifdata(self, value):
        """set exifdata value"""
        conn = self._conn
        c = conn.cursor()
        c.execute(
            "UPDATE export_data SET exifdata = ? WHERE filepath_normalized = ?;",
            (
                value,
                self._filepath_normalized,
            ),
        )
        if not self._context_manager:
            conn.commit()

    @property
    def src_sig(self):
        """return source file signature value"""
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT src_mode, src_size, src_mtime FROM export_data WHERE filepath_normalized = ?;",
            (self._filepath_normalized,),
        ).fetchone()
        if row:
            mtime = int(row[2]) if row[2] is not None else None
            return (row[0], row[1], mtime)

        raise ValueError(
            f"No src_sig found in database for {self._filepath_normalized}"
        )

    @src_sig.setter
    def src_sig(self, value):
        """set source file signature value"""
        conn = self._conn
        c = conn.cursor()
        c.execute(
            "UPDATE export_data SET src_mode = ?, src_size = ?, src_mtime = ? WHERE filepath_normalized = ?;",
            (
                value[0],
                value[1],
                value[2],
                self._filepath_normalized,
            ),
        )
        if not self._context_manager:
            conn.commit()

    @property
    def dest_sig(self):
        """return destination file signature"""
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT dest_mode, dest_size, dest_mtime FROM export_data WHERE filepath_normalized = ?;",
            (self._filepath_normalized,),
        ).fetchone()
        if row:
            mtime = int(row[2]) if row[2] is not None else None
            return (row[0], row[1], mtime)

        raise ValueError(
            f"No dest_sig found in database for {self._filepath_normalized}"
        )

    @dest_sig.setter
    def dest_sig(self, value):
        """set destination file signature"""
        conn = self._conn
        c = conn.cursor()
        c.execute(
            "UPDATE export_data SET dest_mode = ?, dest_size = ?, dest_mtime = ? WHERE filepath_normalized = ?;",
            (
                value[0],
                value[1],
                value[2],
                self._filepath_normalized,
            ),
        )
        if not self._context_manager:
            conn.commit()

    @property
    def photoinfo(self):
        """Returns info value"""
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT photoinfo from photoinfo where uuid = ?;",
            (self.uuid,),
        ).fetchone()
        return row[0] if row else None

    @photoinfo.setter
    def photoinfo(self, value):
        """Sets info value"""
        conn = self._conn
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO photoinfo (uuid, photoinfo) VALUES (?, ?);",
            (self.uuid, value),
        )
        if not self._context_manager:
            conn.commit()

    @property
    def export_options(self):
        """Get export_options value"""
        conn = self._conn
        c = conn.cursor()
        row = c.execute(
            "SELECT export_options from export_data where filepath_normalized = ?;",
            (self._filepath_normalized,),
        ).fetchone()
        return row[0] if row else None

    @export_options.setter
    def export_options(self, value):
        """Set export_options value"""
        conn = self._conn
        c = conn.cursor()
        c.execute(
            "UPDATE export_data SET export_options = ? WHERE filepath_normalized = ?;",
            (value, self._filepath_normalized),
        )
        if not self._context_manager:
            conn.commit()

    def asdict(self):
        """Return dict of self"""
        exifdata = json.loads(self.exifdata) if self.exifdata else None
        photoinfo = json.loads(self.photoinfo) if self.photoinfo else None
        return {
            "filepath": self.filepath,
            "filepath_normalized": self.filepath_normalized,
            "uuid": self.uuid,
            "digest": self.digest,
            "src_sig": self.src_sig,
            "dest_sig": self.dest_sig,
            "export_options": self.export_options,
            "exifdata": exifdata,
            "photoinfo": photoinfo,
        }

    def __enter__(self):
        self._context_manager = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._context_manager = False
