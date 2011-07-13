"""
Copyright
=========
    - Copyright: 2008-2011 Ad-Mail, Inc -- All rights reserved.
    - Author: Ethan Furman
    - Contact: ethanf@admailinc.com
    - Organization: Ad-Mail, Inc.
    - Version: 0.88.019 as of 10 Mar 2011

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    - Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    - Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    - Neither the name of Ad-Mail, Inc nor the
      names of its contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY Ad-Mail, Inc ''AS IS'' AND ANY
EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL Ad-Mail, Inc BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

B{I{Summary}}

Python package for reading/writing dBase III and VFP 6 tables and memos

The entire table is read into memory, and all operations occur on the in-memory
table, with data changes being written to disk as they occur.

Goals:  programming style with databases
    - C{table = dbf.table('table name' [, fielddesc[, fielddesc[, ....]]])}
        - fielddesc examples:  C{name C(30); age N(3,0); wisdom M; marriage D}
    - C{record = [ table.current() | table[int] | table.append() | table.[next|prev|top|bottom|goto]() ]}
    - C{record.field | record['field']} accesses the field

NOTE:  Of the VFP data types, auto-increment and null settings are not implemented.
"""
version = (0, 88, 22)
__docformat__ = 'epytext'

__all__ = (
        'Table', 'List', 'Date', 'DateTime', 'Time',
        'DbfError', 'DataOverflow', 'FieldMissing', 'NonUnicode',
        'DbfWarning', 'Eof', 'Bof', 'DoNotIndex',
        )


import codecs
import csv
import datetime
import locale
import os
import struct
import sys
import time
import unicodedata
import weakref

from array import array
from bisect import bisect_left, bisect_right
from decimal import Decimal
from math import floor
from shutil import copyfileobj

__metaclass__ = type

input_decoding = locale.getdefaultlocale()[1]    # treat non-unicode data as ...
default_codepage = input_decoding  # if no codepage specified on dbf creation, use this
return_ascii = False         # if True -- convert back to icky ascii, losing chars if no mapping
temp_dir = os.environ.get("DBF_TEMP") or os.environ.get("TEMP") or ""

default_type = 'db3'    # default format if none specified
sql_user_functions = {}      # user-defined sql functions

# 2.6+ property for 2.5-
if sys.version_info[:2] < (2, 6):
    # define our own property type
    class property():
        "Emulate PyProperty_Type() in Objects/descrobject.c"
    
        def __init__(self, fget=None, fset=None, fdel=None, doc=None):
            self.fget = fget
            self.fset = fset
            self.fdel = fdel
            self.__doc__ = doc or fget.__doc__
        def __call__(self, func):
            self.fget = func
            if not self.__doc__:
                self.__doc__ = fget.__doc__
        def __get__(self, obj, objtype=None):
            if obj is None:
                return self         
            if self.fget is None:
                raise AttributeError("unreadable attribute")
            return self.fget(obj)
        def __set__(self, obj, value):
            if self.fset is None:
                raise AttributeError("can't set attribute")
            self.fset(obj, value)
        def __delete__(self, obj):
            if self.fdel is None:
                raise AttributeError("can't delete attribute")
            self.fdel(obj)
        def setter(self, func):
            self.fset = func
            return self
        def deleter(self, func):
            self.fdel = func
            return self

# warnings and errors

class DbfError(Exception):
    "Fatal errors elicit this response."
    pass
class DataOverflow(DbfError):
    "Data too large for field"
    def __init__(yo, message, data=None):
        super(DataOverflow, yo).__init__(message)
        yo.data = data
class FieldMissing(KeyError, DbfError):
    "Field does not exist in table"
    def __init__(yo, fieldname):
        super(FieldMissing, yo).__init__('%s:  no such field in table' % fieldname)
        yo.data = fieldname
class NonUnicode(DbfError):
    "Data for table not in unicode"
    def __init__(yo, message=None):
        super(NonUnicode, yo).__init__(message)
class DbfWarning(Exception):
    "Normal operations elicit this response"
class Eof(DbfWarning, StopIteration):
    "End of file reached"
    message = 'End of file reached'
    def __init__(yo):
        super(Eof, yo).__init__(yo.message)
class Bof(DbfWarning, StopIteration):
    "Beginning of file reached"
    message = 'Beginning of file reached'
    def __init__(yo):
        super(Bof, yo).__init__(yo.message)
class DoNotIndex(DbfWarning):
    "Returned by indexing functions to suppress a record from becoming part of the index"
    message = 'Not indexing record'
    def __init__(yo):
        super(DoNotIndex, yo).__init__(yo.message)
# wrappers around datetime and logical objects to allow null values

class Date():
    "adds null capable datetime.date constructs"
    __slots__ = ['_date']
    def __new__(cls, year=None, month=0, day=0):
        """date should be either a datetime.date, a string in yyyymmdd format, 
        or date/month/day should all be appropriate integers"""
        nd = object.__new__(cls)
        nd._date = False
        if type(year) == datetime.date:
            nd._date = year
        elif type(year) == Date:
            nd._date = year._date
        elif year == 'no date':
            pass    # date object is already False
        elif year is not None:
            nd._date = datetime.date(year, month, day)
        return nd
    def __add__(yo, other):
        if yo and type(other) == datetime.timedelta:
            return Date(yo._date + other)
        else:
            return NotImplemented
    def __eq__(yo, other):
        if yo:
            if type(other) == datetime.date:
                return yo._date == other
            elif type(other) == Date:
                if other:
                    return yo._date == other._date
                return False
        else:
            if type(other) == datetime.date:
                return False
            elif type(other) == Date:
                if other:
                    return False
                return True
        return NotImplemented
    def __getattr__(yo, name):
        if yo:
            attribute = yo._date.__getattribute__(name)
            return attribute
        else:
            raise AttributeError('null Date object has no attribute %s' % name)
    def __ge__(yo, other):
        if yo:
            if type(other) == datetime.date:
                return yo._date >= other
            elif type(other) == Date:
                if other:
                    return yo._date >= other._date
                return False
        else:
            if type(other) == datetime.date:
                return False
            elif type(other) == Date:
                if other:
                    return False
                return True
        return NotImplemented
    def __gt__(yo, other):
        if yo:
            if type(other) == datetime.date:
                return yo._date > other
            elif type(other) == Date:
                if other:
                    return yo._date > other._date
                return True
        else:
            if type(other) == datetime.date:
                return False
            elif type(other) == Date:
                if other:
                    return False
                return False
        return NotImplemented
    def __hash__(yo):
        return yo._date.__hash__()
    def __le__(yo, other):
        if yo:
            if type(other) == datetime.date:
                return yo._date <= other
            elif type(other) == Date:
                if other:
                    return yo._date <= other._date
                return False
        else:
            if type(other) == datetime.date:
                return True
            elif type(other) == Date:
                if other:
                    return True
                return True
        return NotImplemented
    def __lt__(yo, other):
        if yo:
            if type(other) == datetime.date:
                return yo._date < other
            elif type(other) == Date:
                if other:
                    return yo._date < other._date
                return False
        else:
            if type(other) == datetime.date:
                return True
            elif type(other) == Date:
                if other:
                    return True
                return False
        return NotImplemented
    def __ne__(yo, other):
        if yo:
            if type(other) == datetime.date:
                return yo._date != other
            elif type(other) == Date:
                if other:
                    return yo._date != other._date
                return True
        else:
            if type(other) == datetime.date:
                return True
            elif type(other) == Date:
                if other:
                    return True
                return False
        return NotImplemented
    def __nonzero__(yo):
        if yo._date:
            return True
        return False
    __radd__ = __add__
    def __rsub__(yo, other):
        if yo and type(other) == datetime.date:
            return other - yo._date
        elif yo and type(other) == Date:
            return other._date - yo._date
        elif yo and type(other) == datetime.timedelta:
            return Date(other - yo._date)
        else:
            return NotImplemented
    def __repr__(yo):
        if yo:
            return "Date(%d, %d, %d)" % yo.timetuple()[:3]
        else:
            return "Date()"
    def __str__(yo):
        if yo:
            return yo.isoformat()
        return "no date"
    def __sub__(yo, other):
        if yo and type(other) == datetime.date:
            return yo._date - other
        elif yo and type(other) == Date:
            return yo._date - other._date
        elif yo and type(other) == datetime.timedelta:
            return Date(yo._date - other)
        else:
            return NotImplemented
    def date(yo):
        if yo:
            return yo._date
        return None
    @classmethod
    def fromordinal(cls, number):
        if number:
            return cls(datetime.date.fromordinal(number))
        return cls()
    @classmethod
    def fromtimestamp(cls, timestamp):
        return cls(datetime.date.fromtimestamp(timestamp))
    @classmethod
    def fromymd(cls, yyyymmdd):
        if yyyymmdd in ('', '        ','no date'):
            return cls()
        return cls(datetime.date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:])))
    def strftime(yo, format):
        if yo:
            return yo._date.strftime(format)
        return '<no date>'
    @classmethod
    def today(cls):
        return cls(datetime.date.today())
    def ymd(yo):
        if yo:
            return "%04d%02d%02d" % yo.timetuple()[:3]
        else:
            return '        '
Date.max = Date(datetime.date.max)
Date.min = Date(datetime.date.min)
class DateTime():
    "adds null capable datetime.datetime constructs"
    __slots__ = ['_datetime']
    def __new__(cls, year=None, month=0, day=0, hour=0, minute=0, second=0, microsec=0):
        """year may be a datetime.datetime"""
        ndt = object.__new__(cls)
        ndt._datetime = False
        if type(year) == datetime.datetime:
            ndt._datetime = year
        elif type(year) == DateTime:
            ndt._datetime = year._datetime
        elif year is not None:
            ndt._datetime = datetime.datetime(year, month, day, hour, minute, second, microsec)
        return ndt
    def __add__(yo, other):
        if yo and type(other) == datetime.timedelta:
            return DateTime(yo._datetime + other)
        else:
            return NotImplemented
    def __eq__(yo, other):
        if yo:
            if type(other) == datetime.datetime:
                return yo._datetime == other
            elif type(other) == DateTime:
                if other:
                    return yo._datetime == other._datetime
                return False
        else:
            if type(other) == datetime.datetime:
                return False
            elif type(other) == DateTime:
                if other:
                    return False
                return True
        return NotImplemented
    def __getattr__(yo, name):
        if yo:
            attribute = yo._datetime.__getattribute__(name)
            return attribute
        else:
            raise AttributeError('null DateTime object has no attribute %s' % name)
    def __ge__(yo, other):
        if yo:
            if type(other) == datetime.datetime:
                return yo._datetime >= other
            elif type(other) == DateTime:
                if other:
                    return yo._datetime >= other._datetime
                return False
        else:
            if type(other) == datetime.datetime:
                return False
            elif type(other) == DateTime:
                if other:
                    return False
                return True
        return NotImplemented
    def __gt__(yo, other):
        if yo:
            if type(other) == datetime.datetime:
                return yo._datetime > other
            elif type(other) == DateTime:
                if other:
                    return yo._datetime > other._datetime
                return True
        else:
            if type(other) == datetime.datetime:
                return False
            elif type(other) == DateTime:
                if other:
                    return False
                return False
        return NotImplemented
    def __hash__(yo):
        return yo._datetime.__hash__()
    def __le__(yo, other):
        if yo:
            if type(other) == datetime.datetime:
                return yo._datetime <= other
            elif type(other) == DateTime:
                if other:
                    return yo._datetime <= other._datetime
                return False
        else:
            if type(other) == datetime.datetime:
                return True
            elif type(other) == DateTime:
                if other:
                    return True
                return True
        return NotImplemented
    def __lt__(yo, other):
        if yo:
            if type(other) == datetime.datetime:
                return yo._datetime < other
            elif type(other) == DateTime:
                if other:
                    return yo._datetime < other._datetime
                return False
        else:
            if type(other) == datetime.datetime:
                return True
            elif type(other) == DateTime:
                if other:
                    return True
                return False
        return NotImplemented
    def __ne__(yo, other):
        if yo:
            if type(other) == datetime.datetime:
                return yo._datetime != other
            elif type(other) == DateTime:
                if other:
                    return yo._datetime != other._datetime
                return True
        else:
            if type(other) == datetime.datetime:
                return True
            elif type(other) == DateTime:
                if other:
                    return True
                return False
        return NotImplemented
    def __nonzero__(yo):
        if yo._datetime is not False:
            return True
        return False
    __radd__ = __add__
    def __rsub__(yo, other):
        if yo and type(other) == datetime.datetime:
            return other - yo._datetime
        elif yo and type(other) == DateTime:
            return other._datetime - yo._datetime
        elif yo and type(other) == datetime.timedelta:
            return DateTime(other - yo._datetime)
        else:
            return NotImplemented
    def __repr__(yo):
        if yo:
            return "DateTime(%d, %d, %d, %d, %d, %d, %d, %d, %d)" % yo._datetime.timetuple()[:]
        else:
            return "DateTime()"
    def __str__(yo):
        if yo:
            return yo.isoformat()
        return "no datetime"
    def __sub__(yo, other):
        if yo and type(other) == datetime.datetime:
            return yo._datetime - other
        elif yo and type(other) == DateTime:
            return yo._datetime - other._datetime
        elif yo and type(other) == datetime.timedelta:
            return DateTime(yo._datetime - other)
        else:
            return NotImplemented
    @classmethod
    def combine(cls, date, time):
        if Date(date) and Time(time):
            return cls(date.year, date.month, date.day, time.hour, time.minute, time.second, time.microsecond)
        return cls()
    def date(yo):
        if yo:
            return Date(yo.year, yo.month, yo.day)
        return Date()
    def datetime(yo):
        if yo:
            return yo._datetime
        return None
    @classmethod    
    def fromordinal(cls, number):
        if number:
            return cls(datetime.datetime.fromordinal(number))
        else:
            return cls()
    @classmethod
    def fromtimestamp(cls, timestamp):
        return DateTime(datetime.datetime.fromtimestamp(timestamp))
    @classmethod
    def now(cls):
        return cls(datetime.datetime.now())
    def time(yo):
        if yo:
            return Time(yo.hour, yo.minute, yo.second, yo.microsecond)
        return Time()
    @classmethod
    def utcnow(cls):
        return cls(datetime.datetime.utcnow())
    @classmethod
    def today(cls):
        return cls(datetime.datetime.today())
DateTime.max = DateTime(datetime.datetime.max)
DateTime.min = DateTime(datetime.datetime.min)
class Time():
    "adds null capable datetime.time constructs"
    __slots__ = ['_time']
    def __new__(cls, hour=None, minute=0, second=0, microsec=0):
        """hour may be a datetime.time"""
        nt = object.__new__(cls)
        nt._time = False
        if type(hour) == datetime.time:
            nt._time = hour
        elif type(hour) == Time:
            nt._time = hour._time
        elif hour is not None:
            nt._time = datetime.time(hour, minute, second, microsec)
        return nt
    def __add__(yo, other):
        if yo and type(other) == datetime.timedelta:
            return Time(yo._time + other)
        else:
            return NotImplemented
    def __eq__(yo, other):
        if yo:
            if type(other) == datetime.time:
                return yo._time == other
            elif type(other) == Time:
                if other:
                    return yo._time == other._time
                return False
        else:
            if type(other) == datetime.time:
                return False
            elif type(other) == Time:
                if other:
                    return False
                return True
        return NotImplemented
    def __getattr__(yo, name):
        if yo:
            attribute = yo._time.__getattribute__(name)
            return attribute
        else:
            raise AttributeError('null Time object has no attribute %s' % name)
    def __ge__(yo, other):
        if yo:
            if type(other) == datetime.time:
                return yo._time >= other
            elif type(other) == Time:
                if other:
                    return yo._time >= other._time
                return False
        else:
            if type(other) == datetime.time:
                return False
            elif type(other) == Time:
                if other:
                    return False
                return True
        return NotImplemented
    def __gt__(yo, other):
        if yo:
            if type(other) == datetime.time:
                return yo._time > other
            elif type(other) == DateTime:
                if other:
                    return yo._time > other._time
                return True
        else:
            if type(other) == datetime.time:
                return False
            elif type(other) == Time:
                if other:
                    return False
                return False
        return NotImplemented
    def __hash__(yo):
        return yo._datetime.__hash__()
    def __le__(yo, other):
        if yo:
            if type(other) == datetime.time:
                return yo._time <= other
            elif type(other) == Time:
                if other:
                    return yo._time <= other._time
                return False
        else:
            if type(other) == datetime.time:
                return True
            elif type(other) == Time:
                if other:
                    return True
                return True
        return NotImplemented
    def __lt__(yo, other):
        if yo:
            if type(other) == datetime.time:
                return yo._time < other
            elif type(other) == Time:
                if other:
                    return yo._time < other._time
                return False
        else:
            if type(other) == datetime.time:
                return True
            elif type(other) == Time:
                if other:
                    return True
                return False
        return NotImplemented
    def __ne__(yo, other):
        if yo:
            if type(other) == datetime.time:
                return yo._time != other
            elif type(other) == Time:
                if other:
                    return yo._time != other._time
                return True
        else:
            if type(other) == datetime.time:
                return True
            elif type(other) == Time:
                if other:
                    return True
                return False
        return NotImplemented
    def __nonzero__(yo):
        if yo._time is not False:
            return True
        return False
    __radd__ = __add__
    def __rsub__(yo, other):
        if yo and type(other) == datetime.time:
            return other - yo._time
        elif yo and type(other) == Time:
            return other._time - yo._time
        elif yo and type(other) == datetime.timedelta:
            return Time(other - yo._datetime)
        else:
            return NotImplemented
    def __repr__(yo):
        if yo:
            return "Time(%d, %d, %d, %d)" % (yo.hour, yo.minute, yo.second, yo.microsecond)
        else:
            return "Time()"
    def __str__(yo):
        if yo:
            return yo.isoformat()
        return "no time"
    def __sub__(yo, other):
        if yo and type(other) == datetime.time:
            return yo._time - other
        elif yo and type(other) == Time:
            return yo._time - other._time
        elif yo and type(other) == datetime.timedelta:
            return Time(yo._time - other)
        else:
            return NotImplemented
Time.max = Time(datetime.time.max)
Time.min = Time(datetime.time.min)

class Logical():
    "return type for Logical fields; implements boolean algebra"
    _need_init = True
    def A(x, y):
        "OR (disjunction): x | y => True iff at least one of x, y is True"
        if not isinstance(y, (x.__class__, bool, type(None))):
            return NotImplemented
        if x.value is None or y == None:
            return x.unknown
        elif x.value is True or y == True:
            return x.true
        return x.false
    def _C_material(x, y):
        "IMP (material implication) x >> y => False iff x == True and y == False"
        if not isinstance(y, (x.__class__, bool, type(None))):
            return NotImplemented
        if x.value is None or y == None:
            return x.unknown
        elif y == False and x.value is True:
            return x.false
        return x.true
    def _C_material_reversed(y, x):
        "IMP (material implication) x >> y => False iff x = True and y = False"
        if not isinstance(x, (y.__class__, bool, type(None))):
            return NotImplemented
        if x == None or y.value is None:
            return y.unknown
        elif x == True and y.value is False:
            return y.false
        return y.true
    def _C_relevant(x, y):
        "IMP (relevant implication) x >> y => True iff both x, y are True, False iff x == True and y == False, Unknown if x is False"
        if not isinstance(y, (x.__class__, bool, type(None))):
            return NotImplemented
        if x.value is True and y == True:
            return x.true
        if x.value is True and y == False:
            return x.false
        return x.unknown
    def _C_relevant_reversed(y, x):
        "IMP (relevant implication) x >> y => True iff both x, y are True, False iff x == True and y == False, Unknown if y is False"
        if not isinstance(x, (y.__class__, bool, type(None))):
            return NotImplemented
        if x == True and y.value is True:
            return y.true
        if  x == True and y.value is False:
            return y.false
        return y.unknown
    def D(x, y):
        "NAND (negative AND) x.D(y): False iff x and y are both True"
        if not isinstance(y, (x.__class__, bool, type(None))):
            return NotImplemented
        if x.value is None or y == None:
            return x.unknown
        elif x.value is True and y == True:
            return x.false
        return x.true
    def E(x, y):
        "EQV (equivalence) x.E(y): True iff x and y are the same"
        if not isinstance(y, (x.__class__, bool, type(None))):
            return NotImplemented
        if x.value is None or y == None:
            return x.unknown
        elif y == True:
            return (x.false, x.true)[x]
        elif y == False:
            return (x.true, x.false)[x]
    def J(x, y):
        "XOR (parity) x ^ y: True iff only one of x,y is True"
        if not isinstance(y, (x.__class__, bool, type(None))):
            return NotImplemented
        if x.value is None or y == None:
            return x.unknown
        elif y == True:
            return (x.true, x.false)[x]
        elif y == False:
            return (x.false, x.true)[x]
    def K(x, y):
        "AND (conjunction) x & y: True iff both x, y are True"
        if not isinstance(y, (x.__class__, bool, type(None))):
            return NotImplemented
        if x.value is None or y == None:
            return x.unknown
        elif y == True:
            return (x.false, x.true)[x]
        elif y == False:
            return x.false
    def N(x):
        "NEG (negation) -x: True iff x = False"
        if x is x.true:
            return x.false
        elif x is x.false:
            return x.true
        else:
            return x.unknown
    @classmethod
    def set_implication(cls, method):
        "sets IMP to material or relevant"
        if not isinstance(method, (str, unicode)) or method.lower() not in ('material','relevant'):
            raise ValueError("method should be 'material' (for strict boolean) or 'relevant', not %r'" % method)
        if method.lower() == 'material':
            cls.C = cls._C_material
            cls.__rshift__ = cls._C_material
            cls.__rrshift__ = cls._C_material_reversed
        elif method.lower() == 'relevant':
            cls.C = cls._C_relevant
            cls.__rshift__ = cls._C_relevant
            cls.__rrshift__ = cls._C_relevant_reversed
    def __new__(cls, value=None):
        if value is None:
            return cls.unknown
        elif isinstance(value, (str, unicode)):
            if value.lower() in ('t','true','y','yes','on'):
                return cls.true
            elif value.lower() in ('f','false','n','no','off'):
                return cls.false
            elif value.lower() in ('?','unknown','null','none',' '):
                return cls.unknown
            else:
                raise ValueError('unknown value for Logical: %s' % value)
        else:
            return (cls.false, cls.true)[bool(value)]
    def __eq__(x, y):
        if isinstance(y, (bool, type(None))):
            return x.__class__(x.value == y)
        if isinstance(y, x.__class__):
            return x.__class__(x.value == y.value)
        return NotImplemented
    def __hash__(x):
        return hash(x.value)
    def __index__(x):
        if x.value is False:
            return 0
        if x.value is True:
            return 1
        if x.value is None:
            return 2
    def __ne__(x, y):
        if isinstance(y, (bool, type(None))):
            return x.__class__(x.value != y)
        if isinstance(y, x.__class__):
            return x.__class__(x.value != y.value)
        return NotImplemented
    def __nonzero__(x):
        return x.value == True
    def __repr__(x):
        return "Logical(%r)" % x.string
    def __str__(x):
        return x.string
    __add__ = A
    __and__ = K
    __mul__ = K
    __neg__ = N
    __or__ = A
    __radd__ = A
    __rand__ = K
    __rshift__ = None
    __rmul__ = K
    __ror__ = A
    __rrshift__ = None
    __rxor__ = J
    __xor__ = J
if hasattr(Logical, '_need_init'):
    Logical.true = true = object.__new__(Logical)
    true.value = True
    true.string = 'T'
    Logical.false = false = object.__new__(Logical)
    false.value = False
    false.string = 'F'
    Logical.unknown = unknown = object.__new__(Logical)
    unknown.value = None
    unknown.string = '?'
    Logical.set_implication('material')
    del Logical._need_init

# Internal classes
class _DbfRecord():
    """Provides routines to extract and save data within the fields of a dbf record."""
    __slots__ = ['_recnum', '_layout', '_data', '_dirty', '__weakref__']
    def _retrieveFieldValue(yo, record_data, fielddef):
        """calls appropriate routine to fetch value stored in field from array
        @param record_data: the data portion of the record
        @type record_data: array of characters
        @param fielddef: description of the field definition
        @type fielddef: dictionary with keys 'type', 'start', 'length', 'end', 'decimals', and 'flags'
        @returns: python data stored in field"""

        field_type = fielddef['type']
        classtype = yo._layout.fieldtypes[field_type]['Class']
        retrieve = yo._layout.fieldtypes[field_type]['Retrieve']
        if classtype is not None:
            datum = retrieve(record_data, fielddef, yo._layout.memo, classtype)
        else:
            datum = retrieve(record_data, fielddef, yo._layout.memo)
        if field_type in yo._layout.character_fields:
            datum = yo._layout.decoder(datum)[0]
            if yo._layout.return_ascii:
                try:
                    datum = yo._layout.output_encoder(datum)[0]
                except UnicodeEncodeError:
                    datum = unicodedata.normalize('NFD', datum).encode('ascii','ignore')
        return datum
    def _updateFieldValue(yo, fielddef, value):
        "calls appropriate routine to convert value to ascii bytes, and save it in record"
        field_type = fielddef['type']
        update = yo._layout.fieldtypes[field_type]['Update']
        if field_type in yo._layout.character_fields:
            if not isinstance(value, unicode):
                if yo._layout.input_decoder is None:
                    raise NonUnicode("String not in unicode format, no default encoding specified")
                value = yo._layout.input_decoder(value)[0]     # input ascii => unicode
            value = yo._layout.encoder(value)[0]           # unicode => table ascii
        bytes = array('c', update(value, fielddef, yo._layout.memo))
        size = fielddef['length']
        if len(bytes) > size:
            raise DataOverflow("tried to store %d bytes in %d byte field" % (len(bytes), size))
        blank = array('c', ' ' * size)
        start = fielddef['start']
        end = start + size
        blank[:len(bytes)] = bytes[:]
        yo._data[start:end] = blank[:]
        yo._dirty = True
    def _update_disk(yo, location='', data=None):
        if not yo._layout.inmemory:
            if yo._recnum < 0:
                raise DbfError("Attempted to update record that has been packed")
            if location == '':
                location = yo._recnum * yo._layout.header.record_length + yo._layout.header.start
            if data is None:
                data = yo._data
            yo._layout.dfd.seek(location)
            yo._layout.dfd.write(data)
            yo._dirty = False
        for index in yo.record_table._indexen:
            index(yo)
    def __contains__(yo, key):
        return key in yo._layout.fields or key in ['record_number','delete_flag']
    def __iter__(yo):
        return (yo[field] for field in yo._layout.fields)
    def __getattr__(yo, name):
        if name[0:2] == '__' and name[-2:] == '__':
            raise AttributeError, 'Method %s is not implemented.' % name
        elif name == 'record_number':
            return yo._recnum
        elif name == 'delete_flag':
            return yo._data[0] != ' '
        elif not name in yo._layout.fields:
            raise FieldMissing(name)
        try:
            fielddef = yo._layout[name]
            value = yo._retrieveFieldValue(yo._data[fielddef['start']:fielddef['end']], fielddef)
            return value
        except DbfError, error:
            error.message = "field --%s-- is %s -> %s" % (name, yo._layout.fieldtypes[fielddef['type']]['Type'], error.message)
            raise
    def __getitem__(yo, item):
        if type(item) in (int, long):
            if not -yo._layout.header.field_count <= item < yo._layout.header.field_count:
                raise IndexError("Field offset %d is not in record" % item)
            return yo[yo._layout.fields[item]]
        elif type(item) == slice:
            sequence = []
            for index in yo._layout.fields[item]:
                sequence.append(yo[index])
            return sequence
        elif type(item) == str:
            return yo.__getattr__(item)
        else:
            raise TypeError("%s is not a field name" % item)
    def __len__(yo):
        return yo._layout.header.field_count
    def __new__(cls, recnum, layout, kamikaze='', _fromdisk=False):
        """record = ascii array of entire record; layout=record specification; memo = memo object for table"""
        record = object.__new__(cls)
        record._dirty = False
        record._recnum = recnum
        record._layout = layout
        if layout.blankrecord is None and not _fromdisk:
            record._createBlankRecord()
        record._data = layout.blankrecord
        if recnum == -1:                    # not a disk-backed record
            return record
        elif type(kamikaze) == array:
            record._data = kamikaze[:]
        elif type(kamikaze) == str:
            record._data = array('c', kamikaze)
        else:
            record._data = kamikaze._data[:]
        datalen = len(record._data)
        if datalen < layout.header.record_length:
            record._data.extend(layout.blankrecord[datalen:])
        elif datalen > layout.header.record_length:
            record._data = record._data[:layout.header.record_length]
        if not _fromdisk and not layout.inmemory:
            record._update_disk()
        return record
    def __setattr__(yo, name, value):
        if name in yo.__slots__:
            object.__setattr__(yo, name, value)
            return
        elif not name in yo._layout.fields:
            raise FieldMissing(name)
        fielddef = yo._layout[name]
        try:
            yo._updateFieldValue(fielddef, value)
        except DbfError, error:
            error.message = "field --%s-- is %s -> %s" % (name, yo._layout.fieldtypes[fielddef['type']]['Type'], error.message)
            error.data = name
            raise
    def __setitem__(yo, name, value):
        if type(name) == str:
            yo.__setattr__(name, value)
        elif type(name) in (int, long):
            yo.__setattr__(yo._layout.fields[name], value)
        elif type(name) == slice:
            sequence = []
            for field in yo._layout.fields[name]:
                sequence.append(field)
            if len(sequence) != len(value):
                raise DbfError("length of slices not equal")
            for field, val in zip(sequence, value):
                yo[field] = val
        else:
            raise TypeError("%s is not a field name" % name)
    def __str__(yo):
        result = []
        for seq, field in enumerate(yo.field_names):
            result.append("%3d - %-10s: %s" % (seq, field, yo[field]))
        return '\n'.join(result)
    def __repr__(yo):
        return yo._data.tostring()
    def _createBlankRecord(yo):
        "creates a blank record data chunk"
        layout = yo._layout
        ondisk = layout.ondisk
        layout.ondisk = False
        yo._data = array('c', ' ' * layout.header.record_length)
        layout.memofields = []
        for field in layout.fields:
            yo._updateFieldValue(layout[field], layout.fieldtypes[layout[field]['type']]['Blank']())
            if layout[field]['type'] in layout.memotypes:
                layout.memofields.append(field)
        layout.blankrecord = yo._data[:]
        layout.ondisk = ondisk
    def delete_record(yo):
        "marks record as deleted"
        yo._data[0] = '*'
        yo._dirty = True
        return yo
    @property
    def field_names(yo):
        "fields in table/record"
        return yo._layout.fields[:]
    def gather_fields(yo, dictionary, drop=False):        # dict, drop_missing=False):
        "saves a dictionary into a record's fields\nkeys with no matching field will raise a FieldMissing exception unless drop_missing = True"
        old_data = yo._data[:]
        try:
            for key in dictionary:
                if not key in yo.field_names:
                    if drop:
                        continue
                    raise FieldMissing(key)
                yo.__setattr__(key, dictionary[key])
        except:
            yo._data[:] = old_data
            raise
        return yo
    @property
    def has_been_deleted(yo):
        "marked for deletion?"
        return yo._data[0] == '*'
    def read_record(yo):
        "refresh record data from disk"
        size = yo._layout.header.record_length
        location = yo._recnum * size + yo._layout.header.start
        yo._layout.dfd.seek(location)
        yo._data[:] = yo._meta.dfd.read(size)
        yo._dirty = False
        return yo
    @property
    def record_number(yo):
        "physical record number"
        return yo._recnum
    @property
    def record_table(yo):
        table = yo._layout.table()
        if table is None:
            raise DbfError("table is no longer available")
        return table
    def check_index(yo):
        for dbfindex in yo._layout.table()._indexen:
            dbfindex(yo)
    def reset_record(yo, keep_fields=None):
        "blanks record"
        if keep_fields is None:
            keep_fields = []
        keep = {}
        for field in keep_fields:
            keep[field] = yo[field]
        if yo._layout.blankrecord == None:
            yo._createBlankRecord()
        yo._data[:] = yo._layout.blankrecord[:]
        for field in keep_fields:
            yo[field] = keep[field]
        yo._dirty = True
        return yo
    def scatter_fields(yo, blank=False):
        "returns a dictionary of fieldnames and values which can be used with gather_fields().  if blank is True, values are empty."
        keys = yo._layout.fields
        if blank:
            values = [yo._layout.fieldtypes[yo._layout[key]['type']]['Blank']() for key in keys]
        else:
            values = [yo[field] for field in keys]
        return dict(zip(keys, values))
    def undelete_record(yo):
        "marks record as active"
        yo._data[0] = ' '
        yo._dirty = True
        return yo
    def write_record(yo, **kwargs):
        "write record data to disk"
        if kwargs:
            yo.gather_fields(kwargs)
        if yo._dirty:
            yo._update_disk()
            return 1
        return 0
class _DbfMemo():
    """Provides access to memo fields as dictionaries
       must override _init, _get_memo, and _put_memo to
       store memo contents to disk"""
    def _init(yo):
        "initialize disk file usage"
    def _get_memo(yo, block):
        "retrieve memo contents from disk"
    def _put_memo(yo, data):
        "store memo contents to disk"
    def __init__(yo, meta):
        ""
        yo.meta = meta
        yo.memory = {}
        yo.nextmemo = 1
        yo._init()
        yo.meta.newmemofile = False
    def get_memo(yo, block, field):
        "gets the memo in block"
        if yo.meta.ignorememos or not block:
            return ''
        if yo.meta.ondisk:
            return yo._get_memo(block)
        else:
            return yo.memory[block]
    def put_memo(yo, data):
        "stores data in memo file, returns block number"
        if yo.meta.ignorememos or data == '':
            return 0
        if yo.meta.inmemory:
            thismemo = yo.nextmemo
            yo.nextmemo += 1
            yo.memory[thismemo] = data
        else:
            thismemo = yo._put_memo(data)
        return thismemo
class _Db3Memo(_DbfMemo):
    def _init(yo):
        "dBase III specific"
        yo.meta.memo_size= 512
        yo.record_header_length = 2
        if yo.meta.ondisk and not yo.meta.ignorememos:
            if yo.meta.newmemofile:
                yo.meta.mfd = open(yo.meta.memoname, 'w+b')
                yo.meta.mfd.write(packLongInt(1) + '\x00' * 508)
            else:
                try:
                    yo.meta.mfd = open(yo.meta.memoname, 'r+b')
                    yo.meta.mfd.seek(0)
                    yo.nextmemo = unpackLongInt(yo.meta.mfd.read(4))
                except:
                    raise DbfError("memo file appears to be corrupt")
    def _get_memo(yo, block):
        block = int(block)
        yo.meta.mfd.seek(block * yo.meta.memo_size)
        eom = -1
        data = ''
        while eom == -1:
            newdata = yo.meta.mfd.read(yo.meta.memo_size)
            if not newdata:
                return data
            data += newdata
            eom = data.find('\x1a\x1a')
        return data[:eom].rstrip()
    def _put_memo(yo, data):
        data = data.rstrip()
        length = len(data) + yo.record_header_length  # room for two ^Z at end of memo
        blocks = length // yo.meta.memo_size
        if length % yo.meta.memo_size:
            blocks += 1
        thismemo = yo.nextmemo
        yo.nextmemo = thismemo + blocks
        yo.meta.mfd.seek(0)
        yo.meta.mfd.write(packLongInt(yo.nextmemo))
        yo.meta.mfd.seek(thismemo * yo.meta.memo_size)
        yo.meta.mfd.write(data)
        yo.meta.mfd.write('\x1a\x1a')
        double_check = yo._get_memo(thismemo)
        if len(double_check) != len(data):
            uhoh = open('dbf_memo_dump.err','wb')
            uhoh.write('thismemo: %d' % thismemo)
            uhoh.write('nextmemo: %d' % yo.nextmemo)
            uhoh.write('saved: %d bytes' % len(data))
            uhoh.write(data)
            uhoh.write('retrieved: %d bytes' % len(double_check))
            uhoh.write(double_check)
            uhoh.close()
            raise DbfError("unknown error: memo not saved")
        return thismemo
class _VfpMemo(_DbfMemo):
    def _init(yo):
        "Visual Foxpro 6 specific"
        if yo.meta.ondisk and not yo.meta.ignorememos:
            yo.record_header_length = 8
            if yo.meta.newmemofile:
                if yo.meta.memo_size == 0:
                    yo.meta.memo_size = 1
                elif 1 < yo.meta.memo_size < 33:
                    yo.meta.memo_size *= 512
                yo.meta.mfd = open(yo.meta.memoname, 'w+b')
                nextmemo = 512 // yo.meta.memo_size
                if nextmemo * yo.meta.memo_size < 512:
                    nextmemo += 1
                yo.nextmemo = nextmemo
                yo.meta.mfd.write(packLongInt(nextmemo, bigendian=True) + '\x00\x00' + \
                        packShortInt(yo.meta.memo_size, bigendian=True) + '\x00' * 504)
            else:
                try:
                    yo.meta.mfd = open(yo.meta.memoname, 'r+b')
                    yo.meta.mfd.seek(0)
                    header = yo.meta.mfd.read(512)
                    yo.nextmemo = unpackLongInt(header[:4], bigendian=True)
                    yo.meta.memo_size = unpackShortInt(header[6:8], bigendian=True)
                except:
                    raise DbfError("memo file appears to be corrupt")
    def _get_memo(yo, block):
        yo.meta.mfd.seek(block * yo.meta.memo_size)
        header = yo.meta.mfd.read(8)
        length = unpackLongInt(header[4:], bigendian=True)
        return yo.meta.mfd.read(length)
    def _put_memo(yo, data):
        data = data.rstrip()     # no trailing whitespace
        yo.meta.mfd.seek(0)
        thismemo = unpackLongInt(yo.meta.mfd.read(4), bigendian=True)
        yo.meta.mfd.seek(0)
        length = len(data) + yo.record_header_length  # room for two ^Z at end of memo
        blocks = length // yo.meta.memo_size
        if length % yo.meta.memo_size:
            blocks += 1
        yo.meta.mfd.write(packLongInt(thismemo+blocks, bigendian=True))
        yo.meta.mfd.seek(thismemo*yo.meta.memo_size)
        yo.meta.mfd.write('\x00\x00\x00\x01' + packLongInt(len(data), bigendian=True) + data)
        return thismemo
class DbfCsv(csv.Dialect):
    "csv format for exporting tables"
    delimiter = ','
    doublequote = True
    escapechar = None
    lineterminator = '\n'
    quotechar = '"'
    skipinitialspace = True
    quoting = csv.QUOTE_NONNUMERIC
csv.register_dialect('dbf', DbfCsv)

# Routines for saving, retrieving, and creating fields

VFPTIME = 1721425

def packShortInt(value, bigendian=False):
        "Returns a two-bye integer from the value, or raises DbfError"
        # 256 / 65,536
        if value > 65535:
            raise DateOverflow("Maximum Integer size exceeded.  Possible: 65535.  Attempted: %d" % value)
        if bigendian:
            return struct.pack('>H', value)
        else:
            return struct.pack('<H', value)
def packLongInt(value, bigendian=False):
        "Returns a four-bye integer from the value, or raises DbfError"
        # 256 / 65,536 / 16,777,216
        if value > 4294967295:
            raise DateOverflow("Maximum Integer size exceeded.  Possible: 4294967295.  Attempted: %d" % value)
        if bigendian:
            return struct.pack('>L', value)
        else:
            return struct.pack('<L', value)
def packDate(date):
        "Returns a group of three bytes, in integer form, of the date"
        return "%c%c%c" % (date.year-1900, date.month, date.day)
def packStr(string):
        "Returns an 11 byte, upper-cased, null padded string suitable for field names; raises DbfError if the string is bigger than 10 bytes"
        if len(string) > 10:
            raise DbfError("Maximum string size is ten characters -- %s has %d characters" % (string, len(string)))
        return struct.pack('11s', string.upper())       
def unpackShortInt(bytes, bigendian=False):
        "Returns the value in the two-byte integer passed in"
        if bigendian:
            return struct.unpack('>H', bytes)[0]
        else:
            return struct.unpack('<H', bytes)[0]
def unpackLongInt(bytes, bigendian=False):
        "Returns the value in the four-byte integer passed in"
        if bigendian:
            return int(struct.unpack('>L', bytes)[0])
        else:
            return int(struct.unpack('<L', bytes)[0])
def unpackDate(bytestr):
        "Returns a Date() of the packed three-byte date passed in"
        year, month, day = struct.unpack('<BBB', bytestr)
        year += 1900
        return Date(year, month, day)
def unpackStr(chars):
        "Returns a normal, lower-cased string from a null-padded byte string"
        field = struct.unpack('%ds' % len(chars), chars)[0]
        name = []
        for ch in field:
            if ch == '\x00':
                break
            name.append(ch.lower())
        return ''.join(name)
def convertToBool(value):
    """Returns boolean true or false; normal rules apply to non-string values; string values
    must be 'y','t', 'yes', or 'true' (case insensitive) to be True"""
    if type(value) == str:
        return bool(value.lower() in ['t', 'y', 'true', 'yes'])
    else:
        return bool(value)
def unsupportedType(something, field, memo=None, typ=None):
    "called if a data type is not supported for that style of table"
    raise DbfError('field type is not supported.')
def retrieveCharacter(bytes, fielddef={}, memo=None, typ=None):
    "Returns the string in bytes with trailing white space removed"
    return typ(bytes.tostring().rstrip())
def updateCharacter(string, fielddef, memo=None):
    "returns the string, truncating if string is longer than it's field"
    string = str(string)
    return string.rstrip()
def retrieveCurrency(bytes, fielddef={}, memo=None, typ=None):
    value = struct.unpack('<q', bytes)[0]
    return typ(("%de-4" % value).strip())
def updateCurrency(value, fielddef={}, memo=None):
    currency = int(value * 10000)
    if not -9223372036854775808 < currency < 9223372036854775808:
        raise DataOverflow("value %s is out of bounds" % value)
    return struct.pack('<q', currency)
def retrieveDate(bytes, fielddef={}, memo=None):
    "Returns the ascii coded date as a Date object"
    return Date.fromymd(bytes.tostring())
def updateDate(moment, fielddef={}, memo=None):
    "returns the Date or datetime.date object ascii-encoded (yyyymmdd)"
    if moment:
        return "%04d%02d%02d" % moment.timetuple()[:3]
    return '        '
def retrieveDouble(bytes, fielddef={}, memo=None, typ=None):
    return float(struct.unpack('<d', bytes)[0])
def updateDouble(value, fielddef={}, memo=None):
    return struct.pack('<d', float(value))
def retrieveInteger(bytes, fielddef={}, memo=None, typ=None):
    "Returns the binary number stored in bytes in little-endian format"
    if typ is None or typ == 'default':
        return struct.unpack('<i', bytes)[0]
    else:
        return typ(struct.unpack('<i', bytes)[0])
def updateInteger(value, fielddef={}, memo=None):
    "returns value in little-endian binary format"
    try:
        value = int(value)
    except Exception:
        raise DbfError("incompatible type: %s(%s)" % (type(value), value))
    if not -2147483648 < value < 2147483647:
        raise DataOverflow("Integer size exceeded.  Possible: -2,147,483,648..+2,147,483,647.  Attempted: %d" % value)
    return struct.pack('<i', int(value))
def retrieveLogical(bytes, fielddef={}, memo=None):
    "Returns True if bytes is 't', 'T', 'y', or 'Y', None if '?', and False otherwise"
    bytes = bytes.tostring()
    if bytes == '?':
        return None
    return bytes in ['t','T','y','Y']
def updateLogical(logical, fielddef={}, memo=None):
    "Returns 'T' if logical is True, 'F' otherwise"
    if type(logical) != bool:
        logical = convertToBool(logical)
    if type(logical) <> bool:
        raise DbfError('Value %s is not logical.' % logical)
    return logical and 'T' or 'F'
def retrieveMemo(bytes, fielddef, memo, typ):
    "Returns the block of data from a memo file"
    stringval = bytes.tostring()
    if stringval.strip():
        block = int(stringval.strip())
    else:
        block = 0
    return memo.get_memo(block, fielddef)
def updateMemo(string, fielddef, memo):
    "Writes string as a memo, returns the block number it was saved into"
    block = memo.put_memo(string)
    if block == 0:
        block = ''
    return "%*s" % (fielddef['length'], block)
def retrieveNumeric(bytes, fielddef, memo=None, typ=None):
    "Returns the number stored in bytes as integer if field spec for decimals is 0, float otherwise"
    string = bytes.tostring()
    if string[0:1] == '*':  # value too big to store (Visual FoxPro idiocy)
        return None
    if not string.strip():
        string = '0'
    if typ == 'default':
        if fielddef['decimals'] == 0:
            return int(string)
        else:
            return float(string)
    else:
        return typ(string.strip())
def updateNumeric(value, fielddef, memo=None):
    "returns value as ascii representation, rounding decimal portion as necessary"
    try:
        value = float(value)
    except Exception:
        raise DbfError("incompatible type: %s(%s)" % (type(value), value))
    decimalsize = fielddef['decimals']
    if decimalsize:
        decimalsize += 1
    maxintegersize = fielddef['length']-decimalsize
    integersize = len("%.0f" % floor(value))
    if integersize > maxintegersize:
        raise DataOverflow('Integer portion too big')
    return "%*.*f" % (fielddef['length'], fielddef['decimals'], value)
def retrieveVfpDateTime(bytes, fielddef={}, memo=None):
    """returns the date/time stored in bytes; dates <= 01/01/1981 00:00:00
    may not be accurate;  BC dates are nulled."""
    # two four-byte integers store the date and time.
    # millesecords are discarded from time
    time = retrieveInteger(bytes[4:])
    microseconds = (time % 1000) * 1000
    time = time // 1000                      # int(round(time, -3)) // 1000 discard milliseconds
    hours = time // 3600
    mins = time % 3600 // 60
    secs = time % 3600 % 60
    time = Time(hours, mins, secs, microseconds)
    possible = retrieveInteger(bytes[:4])
    possible -= VFPTIME
    possible = max(0, possible)
    date = Date.fromordinal(possible)
    return DateTime.combine(date, time)
def updateVfpDateTime(moment, fielddef={}, memo=None):
    """sets the date/time stored in moment
    moment must have fields year, month, day, hour, minute, second, microsecond"""
    bytes = [0] * 8
    hour = moment.hour
    minute = moment.minute
    second = moment.second
    millisecond = moment.microsecond // 1000       # convert from millionths to thousandths
    time = ((hour * 3600) + (minute * 60) + second) * 1000 + millisecond
    bytes[4:] = updateInteger(time)
    bytes[:4] = updateInteger(moment.toordinal() + VFPTIME)
    return ''.join(bytes)
def retrieveVfpMemo(bytes, fielddef, memo, typ=None):
    "Returns the block of data from a memo file"
    block = struct.unpack('<i', bytes)[0]
    return memo.get_memo(block, fielddef)
def updateVfpMemo(string, fielddef, memo):
    "Writes string as a memo, returns the block number it was saved into"
    block = memo.put_memo(string)
    return struct.pack('<i', block)
def addCharacter(format):
    if format[1] != '(' or format[-1] != ')':
        raise DbfError("Format for Character field creation is C(n), not %s" % format)
    length = int(format[2:-1])
    if not 0 < length < 255:
        raise ValueError
    decimals = 0
    return length, decimals
def addDate(format):
    length = 8
    decimals = 0
    return length, decimals
def addLogical(format):
    length = 1
    decimals = 0
    return length, decimals
def addMemo(format):
    length = 10
    decimals = 0
    return length, decimals
def addNumeric(format):
    if format[1] != '(' or format[-1] != ')':
        raise DbfError("Format for Numeric field creation is N(n,n), not %s" % format)
    length, decimals = format[2:-1].split(',')
    length = int(length)
    decimals = int(decimals)
    if not 0 < length < 18:
        raise ValueError
    if decimals and not 0 < decimals <= length - 2:
        raise ValueError
    return length, decimals
def addVfpCurrency(format):
    length = 8
    decimals = 0
    return length, decimals
def addVfpDateTime(format):
    length = 8
    decimals = 8
    return length, decimals
def addVfpDouble(format):
    length = 8
    decimals = 0
    return length, decimals
def addVfpInteger(format):
    length = 4
    decimals = 0
    return length, decimals
def addVfpMemo(format):
    length = 4
    decimals = 0
    return length, decimals
def addVfpNumeric(format):
    if format[1] != '(' or format[-1] != ')':
        raise DbfError("Format for Numeric field creation is N(n,n), not %s" % format)
    length, decimals = format[2:-1].split(',')
    length = int(length)
    decimals = int(decimals)
    if not 0 < length < 21:
        raise ValueError
    if decimals and not 0 < decimals <= length - 2:
        raise ValueError
    return length, decimals

# Public classes
class DbfTable():
    """Provides a framework for dbf style tables."""
    _version = 'basic memory table'
    _versionabbv = 'dbf'
    _fieldtypes = {
            'D' : { 'Type':'Date',    'Init':addDate,    'Blank':Date.today, 'Retrieve':retrieveDate,    'Update':updateDate, 'Class':None},
            'L' : { 'Type':'Logical', 'Init':addLogical, 'Blank':bool,       'Retrieve':retrieveLogical, 'Update':updateLogical, 'Class':None},
            'M' : { 'Type':'Memo',    'Init':addMemo,    'Blank':str,        'Retrieve':retrieveMemo,    'Update':updateMemo, 'Class':None} }
    _memoext = ''
    _memotypes = tuple('M', )
    _memoClass = _DbfMemo
    _yesMemoMask = ''
    _noMemoMask = ''
    _fixed_fields = ('M','D','L')           # always same length in table
    _variable_fields = tuple()              # variable length in table
    _character_fields = tuple('M', )        # field representing character data
    _decimal_fields = tuple()               # text-based numeric fields
    _numeric_fields = tuple()               # fields representing a number
    _currency_fields = tuple()
    _dbfTableHeader = array('c', '\x00' * 32)
    _dbfTableHeader[0] = '\x00'             # table type - none
    _dbfTableHeader[8:10] = array('c', packShortInt(33))
    _dbfTableHeader[10] = '\x01'            # record length -- one for delete flag
    _dbfTableHeader[29] = '\x00'            # code page -- none, using plain ascii
    _dbfTableHeader = _dbfTableHeader.tostring()
    _dbfTableHeaderExtra = ''
    _supported_tables = []
    _read_only = False
    _meta_only = False
    _use_deleted = True
    backup = False
    class _DbfLists():
        "implements the weakref structure for DbfLists"
        def __init__(yo):
            yo._lists = set()
        def __iter__(yo):
            yo._lists = set([s for s in yo._lists if s() is not None])    
            return (s() for s in yo._lists if s() is not None)
        def __len__(yo):
            yo._lists = set([s for s in yo._lists if s() is not None])
            return len(yo._lists)
        def add(yo, new_list):
            yo._lists.add(weakref.ref(new_list))
            yo._lists = set([s for s in yo._lists if s() is not None])
    class _Indexen():
        "implements the weakref structure for seperate indexes"
        def __init__(yo):
            yo._indexen = set()
        def __iter__(yo):
            yo._indexen = set([s for s in yo._indexen if s() is not None])    
            return (s() for s in yo._indexen if s() is not None)
        def __len__(yo):
            yo._indexen = set([s for s in yo._indexen if s() is not None])
            return len(yo._indexen)
        def add(yo, new_list):
            yo._indexen.add(weakref.ref(new_list))
            yo._indexen = set([s for s in yo._indexen if s() is not None])
    class _MetaData(dict):
        blankrecord = None
        fields = None
        filename = None
        dfd = None
        memoname = None
        newmemofile = False
        memo = None
        mfd = None
        ignorememos = False
        memofields = None
        current = -1
    class _TableHeader():
        def __init__(yo, data):
            if len(data) != 32:
                raise DbfError('table header should be 32 bytes, but is %d bytes' % len(data))
            yo._data = array('c', data + '\x0d')
        def codepage(yo, cp=None):
            "get/set code page of table"
            if cp is None:
                return yo._data[29]
            else:
                cp, sd, ld = _codepage_lookup(cp)
                yo._data[29] = cp                    
                return cp
        @property
        def data(yo):
            "main data structure"
            date = packDate(Date.today())
            yo._data[1:4] = array('c', date)
            return yo._data.tostring()
        @data.setter
        def data(yo, bytes):
            if len(bytes) < 32:
                raise DbfError("length for data of %d is less than 32" % len(bytes))
            yo._data[:] = array('c', bytes)
        @property
        def extra(yo):
            "extra dbf info (located after headers, before data records)"
            fieldblock = yo._data[32:]
            for i in range(len(fieldblock)//32+1):
                cr = i * 32
                if fieldblock[cr] == '\x0d':
                    break
            else:
                raise DbfError("corrupt field structure")
            cr += 33    # skip past CR
            return yo._data[cr:].tostring()
        @extra.setter
        def extra(yo, data):
            fieldblock = yo._data[32:]
            for i in range(len(fieldblock)//32+1):
                cr = i * 32
                if fieldblock[cr] == '\x0d':
                    break
            else:
                raise DbfError("corrupt field structure")
            cr += 33    # skip past CR
            yo._data[cr:] = array('c', data)                             # extra
            yo._data[8:10] = array('c', packShortInt(len(yo._data)))  # start
        @property
        def field_count(yo):
            "number of fields (read-only)"
            fieldblock = yo._data[32:]
            for i in range(len(fieldblock)//32+1):
                cr = i * 32
                if fieldblock[cr] == '\x0d':
                    break
            else:
                raise DbfError("corrupt field structure")
            return len(fieldblock[:cr]) // 32
        @property
        def fields(yo):
            "field block structure"
            fieldblock = yo._data[32:]
            for i in range(len(fieldblock)//32+1):
                cr = i * 32
                if fieldblock[cr] == '\x0d':
                    break
            else:
                raise DbfError("corrupt field structure")
            return fieldblock[:cr].tostring()
        @fields.setter
        def fields(yo, block):
            fieldblock = yo._data[32:]
            for i in range(len(fieldblock)//32+1):
                cr = i * 32
                if fieldblock[cr] == '\x0d':
                    break
            else:
                raise DbfError("corrupt field structure")
            cr += 32    # convert to indexing main structure
            fieldlen = len(block)
            if fieldlen % 32 != 0:
                raise DbfError("fields structure corrupt: %d is not a multiple of 32" % fieldlen)
            yo._data[32:cr] = array('c', block)                           # fields
            yo._data[8:10] = array('c', packShortInt(len(yo._data)))   # start
            fieldlen = fieldlen // 32
            recordlen = 1                                     # deleted flag
            for i in range(fieldlen):
                recordlen += ord(block[i*32+16])
            yo._data[10:12] = array('c', packShortInt(recordlen))
        @property
        def record_count(yo):
            "number of records (maximum 16,777,215)"
            return unpackLongInt(yo._data[4:8].tostring())
        @record_count.setter
        def record_count(yo, count):
            yo._data[4:8] = array('c', packLongInt(count))
        @property
        def record_length(yo):
            "length of a record (read_only) (max of 65,535)"
            return unpackShortInt(yo._data[10:12].tostring())
        @property
        def start(yo):
            "starting position of first record in file (must be within first 64K)"
            return unpackShortInt(yo._data[8:10].tostring())
        @start.setter
        def start(yo, pos):
            yo._data[8:10] = array('c', packShortInt(pos))
        @property
        def update(yo):
            "date of last table modification (read-only)"
            return unpackDate(yo._data[1:4].tostring())
        @property
        def version(yo):
            "dbf version"
            return yo._data[0]
        @version.setter
        def version(yo, ver):
            yo._data[0] = ver
    class _Table():
        "implements the weakref table for records"
        def __init__(yo, count, meta):
            yo._meta = meta
            yo._weakref_list = [weakref.ref(lambda x: None)] * count
        def __getitem__(yo, index):
            maybe = yo._weakref_list[index]()
            if maybe is None:
                if index < 0:
                    index += yo._meta.header.record_count
                size = yo._meta.header.record_length
                location = index * size + yo._meta.header.start
                yo._meta.dfd.seek(location)
                if yo._meta.dfd.tell() != location:
                    raise ValueError("unable to seek to offset %d in file" % location)
                bytes = yo._meta.dfd.read(size)
                if not bytes:
                    raise ValueError("unable to read record data from %s at location %d" % (yo._meta.filename, location))
                maybe = _DbfRecord(recnum=index, layout=yo._meta, kamikaze=bytes, _fromdisk=True)
                yo._weakref_list[index] = weakref.ref(maybe)
            return maybe
        def append(yo, record):
            yo._weakref_list.append(weakref.ref(record))
        def clear(yo):
            yo._weakref_list[:] = []
        def pop(yo):
            return yo._weakref_list.pop()
    class DbfIterator():
        "returns records using current index"
        def __init__(yo, table):
            yo._table = table
            yo._index = -1
            yo._more_records = True
        def __iter__(yo):
            return yo
        def next(yo):
            while yo._more_records:
                yo._index += 1
                if yo._index >= len(yo._table):
                    yo._more_records = False
                    continue
                record = yo._table[yo._index]
                if not yo._table.use_deleted and record.has_been_deleted:
                    continue
                return record
            else:
                raise StopIteration
    def _buildHeaderFields(yo):
        "constructs fieldblock for disk table"
        fieldblock = array('c', '')
        memo = False
        yo._meta.header.version = chr(ord(yo._meta.header.version) & ord(yo._noMemoMask))
        for field in yo._meta.fields:
            if yo._meta.fields.count(field) > 1:
                raise DbfError("corrupted field structure (noticed in _buildHeaderFields)")
            fielddef = array('c', '\x00' * 32)
            fielddef[:11] = array('c', packStr(field))
            fielddef[11] = yo._meta[field]['type']
            fielddef[12:16] = array('c', packLongInt(yo._meta[field]['start']))
            fielddef[16] = chr(yo._meta[field]['length'])
            fielddef[17] = chr(yo._meta[field]['decimals'])
            fielddef[18] = chr(yo._meta[field]['flags'])
            fieldblock.extend(fielddef)
            if yo._meta[field]['type'] in yo._meta.memotypes:
                memo = True
        yo._meta.header.fields = fieldblock.tostring()
        if memo:
            yo._meta.header.version = chr(ord(yo._meta.header.version) | ord(yo._yesMemoMask))
            if yo._meta.memo is None:
                yo._meta.memo = yo._memoClass(yo._meta)
    def _checkMemoIntegrity(yo):
        "dBase III specific"
        if yo._meta.header.version == '\x83':
            try:
                yo._meta.memo = yo._memoClass(yo._meta)
            except:
                yo._meta.dfd.close()
                yo._meta.dfd = None
                raise
        if not yo._meta.ignorememos:
            for field in yo._meta.fields:
                if yo._meta[field]['type'] in yo._memotypes:
                    if yo._meta.header.version != '\x83':
                        yo._meta.dfd.close()
                        yo._meta.dfd = None
                        raise DbfError("Table structure corrupt:  memo fields exist, header declares no memos")
                    elif not os.path.exists(yo._meta.memoname):
                        yo._meta.dfd.close()
                        yo._meta.dfd = None
                        raise DbfError("Table structure corrupt:  memo fields exist without memo file")
                    break
    def _initializeFields(yo):
        "builds the FieldList of names, types, and descriptions from the disk file"
        yo._meta.fields[:] = []
        offset = 1
        fieldsdef = yo._meta.header.fields
        if len(fieldsdef) % 32 != 0:
            raise DbfError("field definition block corrupt: %d bytes in size" % len(fieldsdef))
        if len(fieldsdef) // 32 != yo.field_count:
            raise DbfError("Header shows %d fields, but field definition block has %d fields" % (yo.field_count, len(fieldsdef)//32))
        for i in range(yo.field_count):
            fieldblock = fieldsdef[i*32:(i+1)*32]
            name = unpackStr(fieldblock[:11])
            type = fieldblock[11]
            if not type in yo._meta.fieldtypes:
                raise DbfError("Unknown field type: %s" % type)
            start = offset
            length = ord(fieldblock[16])
            offset += length
            end = start + length
            decimals = ord(fieldblock[17])
            flags = ord(fieldblock[18])
            if name in yo._meta.fields:
                raise DbfError('Duplicate field name found: %s' % name)
            yo._meta.fields.append(name)
            yo._meta[name] = {'type':type,'start':start,'length':length,'end':end,'decimals':decimals,'flags':flags}
    def _fieldLayout(yo, i):
        "Returns field information Name Type(Length[,Decimals])"
        name = yo._meta.fields[i]
        type = yo._meta[name]['type']
        length = yo._meta[name]['length']
        decimals = yo._meta[name]['decimals']
        if type in yo._decimal_fields:
            description = "%s %s(%d,%d)" % (name, type, length, decimals)
        elif type in yo._fixed_fields:
            description = "%s %s" % (name, type)
        else:
            description = "%s %s(%d)" % (name, type, length)
        return description
    def _loadtable(yo):
        "loads the records from disk to memory"
        if yo._meta_only:
            raise DbfError("%s has been closed, records are unavailable" % yo.filename)
        dfd = yo._meta.dfd
        header = yo._meta.header
        dfd.seek(header.start)
        allrecords = dfd.read()                     # kludge to get around mysterious errno 0 problems
        dfd.seek(0)
        length = header.record_length
        for i in range(header.record_count):
            record_data = allrecords[length*i:length*i+length]
            yo._table.append(_DbfRecord(i, yo._meta, allrecords[length*i:length*i+length], _fromdisk=True))
        dfd.seek(0)
    def _list_fields(yo, specs, sep=','):
        if specs is None:
            specs = yo.field_names
        elif isinstance(specs, str):
            specs = specs.split(sep)
        else:
            specs = list(specs)
        specs = [s.strip() for s in specs]
        return specs
    def _update_disk(yo, headeronly=False):
        "synchronizes the disk file with current data"
        if yo._meta.inmemory:
            return
        fd = yo._meta.dfd
        fd.seek(0)
        fd.write(yo._meta.header.data)
        if not headeronly:
            for record in yo._table:
                record._update_disk()
                fd.flush()
            fd.truncate(yo._meta.header.start + yo._meta.header.record_count * yo._meta.header.record_length)
        if 'db3' in yo._versionabbv:
            fd.seek(0, os.SEEK_END)
            fd.write('\x1a')        # required for dBase III
            fd.flush()
            fd.truncate(yo._meta.header.start + yo._meta.header.record_count * yo._meta.header.record_length + 1)

    def __contains__(yo, key):
        return key in yo.field_names
    def __enter__(yo):
        return yo
    def __exit__(yo, *exc_info):
        yo.close()
    def __getattr__(yo, name):
        if name in ('_table'):
                if yo._meta.ondisk:
                    yo._table = yo._Table(len(yo), yo._meta)
                else:
                    yo._table = []
                    yo._loadtable()
        return object.__getattribute__(yo, name)
    def __getitem__(yo, value):
        if type(value) == int:
            if not -yo._meta.header.record_count <= value < yo._meta.header.record_count: 
                raise IndexError("Record %d is not in table." % value)
            return yo._table[value]
        elif type(value) == slice:
            sequence = List(desc='%s -->  %s' % (yo.filename, value), field_names=yo.field_names)
            yo._dbflists.add(sequence)
            for index in range(len(yo))[value]:
                record = yo._table[index]
                if yo.use_deleted is True or not record.has_been_deleted:
                    sequence.append(record)
            return sequence
        else:
            raise TypeError('type <%s> not valid for indexing' % type(value))
    def __init__(yo, filename=':memory:', field_specs=None, memo_size=128, ignore_memos=False, 
                 read_only=False, keep_memos=False, meta_only=False, codepage=None, 
                 numbers='default', strings=str, currency=Decimal):
        """open/create dbf file
        filename should include path if needed
        field_specs can be either a ;-delimited string or a list of strings
        memo_size is always 512 for db3 memos
        ignore_memos is useful if the memo file is missing or corrupt
        read_only will load records into memory, then close the disk file
        keep_memos will also load any memo fields into memory
        meta_only will ignore all records, keeping only basic table information
        codepage will override whatever is set in the table itself"""
        if filename[0] == filename[-1] == ':':
            if field_specs is None:
                raise DbfError("field list must be specified for memory tables")
        elif type(yo) is DbfTable:
            raise DbfError("only memory tables supported")
        yo._dbflists = yo._DbfLists()
        yo._indexen = yo._Indexen()
        yo._meta = meta = yo._MetaData()
        for datatypes, classtype in (
                (yo._character_fields, strings),
                (yo._numeric_fields, numbers),
                (yo._currency_fields, currency),
                ):
            for datatype in datatypes:
                yo._fieldtypes[datatype]['Class'] = classtype
        meta.numbers = numbers
        meta.strings = strings
        meta.currency = currency
        meta.table = weakref.ref(yo)
        meta.filename = filename
        meta.fields = []
        meta.fieldtypes = yo._fieldtypes
        meta.fixed_fields = yo._fixed_fields
        meta.variable_fields = yo._variable_fields
        meta.character_fields = yo._character_fields
        meta.decimal_fields = yo._decimal_fields
        meta.numeric_fields = yo._numeric_fields
        meta.memotypes = yo._memotypes
        meta.ignorememos = ignore_memos
        meta.memo_size = memo_size
        meta.input_decoder = codecs.getdecoder(input_decoding)      # from ascii to unicode
        meta.output_encoder = codecs.getencoder(input_decoding)     # and back to ascii
        meta.return_ascii = return_ascii
        meta.header = header = yo._TableHeader(yo._dbfTableHeader)
        header.extra = yo._dbfTableHeaderExtra
        header.data        #force update of date
        if filename[0] == filename[-1] == ':':
            yo._table = []
            meta.ondisk = False
            meta.inmemory = True
            meta.memoname = filename
        else:
            base, ext = os.path.splitext(filename)
            if ext == '':
                meta.filename =  base + '.dbf'
            meta.memoname = base + yo._memoext
            meta.ondisk = True
            meta.inmemory = False
        if field_specs:
            if meta.ondisk:
                meta.dfd = open(meta.filename, 'w+b')
                meta.newmemofile = True
            yo.add_fields(field_specs)
            header.codepage(codepage or default_codepage)
            cp, sd, ld = _codepage_lookup(meta.header.codepage())
            meta.decoder = codecs.getdecoder(sd) 
            meta.encoder = codecs.getencoder(sd)
            return
        try:
            dfd = meta.dfd = open(meta.filename, 'r+b')
        except IOError, e:
            raise DbfError(str(e))
        dfd.seek(0)
        meta.header = header = yo._TableHeader(dfd.read(32))
        if not header.version in yo._supported_tables:
            dfd.close()
            dfd = None
            raise DbfError(
                "%s does not support %s [%x]" % 
                (yo._version,
                version_map.get(meta.header.version, 'Unknown: %s' % meta.header.version),
                ord(meta.header.version)))
        cp, sd, ld = _codepage_lookup(meta.header.codepage())
        yo._meta.decoder = codecs.getdecoder(sd) 
        yo._meta.encoder = codecs.getencoder(sd)
        fieldblock = dfd.read(header.start - 32)
        for i in range(len(fieldblock)//32+1):
            fieldend = i * 32
            if fieldblock[fieldend] == '\x0d':
                break
        else:
            raise DbfError("corrupt field structure in header")
        if len(fieldblock[:fieldend]) % 32 != 0:
            raise DbfError("corrupt field structure in header")
        header.fields = fieldblock[:fieldend]
        header.extra = fieldblock[fieldend+1:]  # skip trailing \r
        yo._initializeFields()
        yo._checkMemoIntegrity()
        meta.current = -1
        if len(yo) > 0:
            meta.current = 0
        dfd.seek(0)
        if meta_only:
            yo.close(keep_table=False, keep_memos=False)
        elif read_only:
            yo.close(keep_table=True, keep_memos=keep_memos)
        if codepage is not None:
            cp, sd, ld = _codepage_lookup(codepage)
            yo._meta.decoder = codecs.getdecoder(sd) 
            yo._meta.encoder = codecs.getencoder(sd)

    def __iter__(yo):
        return yo.DbfIterator(yo)           
    def __len__(yo):
        return yo._meta.header.record_count
    def __nonzero__(yo):
        return yo._meta.header.record_count != 0
    def __repr__(yo):
        if yo._read_only:
            return __name__ + ".Table('%s', read_only=True)" % yo._meta.filename
        elif yo._meta_only:
            return __name__ + ".Table('%s', meta_only=True)" % yo._meta.filename
        else:
            return __name__ + ".Table('%s')" % yo._meta.filename
    def __str__(yo):
        if yo._read_only:
            status = "read-only"
        elif yo._meta_only:
            status = "meta-only"
        else:
            status = "read/write"
        str =  """
        Table:         %s
        Type:          %s
        Codepage:      %s
        Status:        %s
        Last updated:  %s
        Record count:  %d
        Field count:   %d
        Record length: %d """ % (yo.filename, version_map.get(yo._meta.header.version, 
            'unknown - ' + hex(ord(yo._meta.header.version))), yo.codepage, status, 
            yo.last_update, len(yo), yo.field_count, yo.record_length)
        str += "\n        --Fields--\n"
        for i in range(len(yo._meta.fields)):
            str += "%11d) %s\n" % (i, yo._fieldLayout(i))
        return str
    @property
    def codepage(yo):
        return "%s (%s)" % code_pages[yo._meta.header.codepage()]
    @codepage.setter
    def codepage(yo, cp):
        cp = code_pages[yo._meta.header.codepage(cp)][0]
        yo._meta.decoder = codecs.getdecoder(cp) 
        yo._meta.encoder = codecs.getencoder(cp)
        yo._update_disk(headeronly=True)
    @property
    def field_count(yo):
        "the number of fields in the table"
        return yo._meta.header.field_count
    @property
    def field_names(yo):
        "a list of the fields in the table"
        return yo._meta.fields[:]
    @property
    def filename(yo):
        "table's file name, including path (if specified on open)"
        return yo._meta.filename
    @property
    def last_update(yo):
        "date of last update"
        return yo._meta.header.update
    @property
    def memoname(yo):
        "table's memo name (if path included in filename on open)"
        return yo._meta.memoname
    @property
    def record_length(yo):
        "number of bytes in a record"
        return yo._meta.header.record_length
    @property
    def record_number(yo):
        "index number of the current record"
        return yo._meta.current
    @property
    def supported_tables(yo):
        "allowable table types"
        return yo._supported_tables
    @property
    def use_deleted(yo):
        "process or ignore deleted records"
        return yo._use_deleted
    @use_deleted.setter
    def use_deleted(yo, new_setting):
        yo._use_deleted = new_setting
    @property
    def version(yo):
        "returns the dbf type of the table"
        return yo._version
    def add_fields(yo, field_specs):
        """adds field(s) to the table layout; format is Name Type(Length,Decimals)[; Name Type(Length,Decimals)[...]]
        backup table is created with _backup appended to name
        then modifies current structure"""
        all_records = [record for record in yo]
        if yo:
            yo.create_backup()
        yo._meta.blankrecord = None
        meta = yo._meta
        offset = meta.header.record_length
        fields = yo._list_fields(field_specs, sep=';')
        for field in fields:
            try:
                name, format = field.split()
                if name[0] == '_' or name[0].isdigit() or not name.replace('_','').isalnum():
                    raise DbfError("%s invalid:  field names must start with a letter, and can only contain letters, digits, and _" % name)
                name = name.lower()
                if name in meta.fields:
                    raise DbfError("Field '%s' already exists" % name)
                field_type = format[0].upper()
                if len(name) > 10:
                    raise DbfError("Maximum field name length is 10.  '%s' is %d characters long." % (name, len(name)))
                if not field_type in meta.fieldtypes.keys():
                    raise DbfError("Unknown field type:  %s" % field_type)
                length, decimals = yo._meta.fieldtypes[field_type]['Init'](format)
            except ValueError:
                raise DbfError("invalid field specifier: %s (multiple fields should be separated with ';'" % field)
            start = offset
            end = offset + length
            offset = end
            meta.fields.append(name)
            meta[name] = {'type':field_type, 'start':start, 'length':length, 'end':end, 'decimals':decimals, 'flags':0}
            if meta[name]['type'] in yo._memotypes and meta.memo is None:
                meta.memo = yo._memoClass(meta)
            for record in yo:
                record[name] = meta.fieldtypes[field_type]['Blank']()
        yo._buildHeaderFields()
        yo._update_disk()
    def append(yo, kamikaze='', drop=False, multiple=1):
        "adds <multiple> blank records, and fills fields with dict/tuple values if present"
        if not yo.field_count:
            raise DbfError("No fields defined, cannot append")
        empty_table = len(yo) == 0
        dictdata = False
        tupledata = False
        if not isinstance(kamikaze, _DbfRecord):
            if isinstance(kamikaze, dict):
                dictdata = kamikaze
                kamikaze = ''
            elif isinstance(kamikaze, tuple):
                tupledata = kamikaze
                kamikaze = ''
        newrecord = _DbfRecord(recnum=yo._meta.header.record_count, layout=yo._meta, kamikaze=kamikaze)
        yo._table.append(newrecord)
        yo._meta.header.record_count += 1
        try:
            if dictdata:
                newrecord.gather_fields(dictdata, drop=drop)
            elif tupledata:
                for index, item in enumerate(tupledata):
                    newrecord[index] = item
            elif kamikaze == str:
                for field in yo._meta.memofields:
                    newrecord[field] = ''
            elif kamikaze:
                for field in yo._meta.memofields:
                    newrecord[field] = kamikaze[field]
            newrecord.write_record()
        except Exception:
            yo._table.pop()     # discard failed record
            yo._meta.header.record_count = yo._meta.header.record_count - 1
            yo._update_disk()
            raise
        multiple -= 1
        if multiple:
            data = newrecord._data
            single = yo._meta.header.record_count
            total = single + multiple
            while single < total:
                multi_record = _DbfRecord(single, yo._meta, kamikaze=data)
                yo._table.append(multi_record)
                for field in yo._meta.memofields:
                    multi_record[field] = newrecord[field]
                single += 1
                multi_record.write_record()
            yo._meta.header.record_count = total   # += multiple
            yo._meta.current = yo._meta.header.record_count - 1
            newrecord = multi_record
        yo._update_disk(headeronly=True)
        if empty_table:
            yo._meta.current = 0
        return newrecord
    def bof(yo, _move=False):
        "moves record pointer to previous usable record; returns True if no more usable records"
        current = yo._meta.current
        try:
            while yo._meta.current > 0:
                yo._meta.current -= 1
                if yo.use_deleted or not yo.current().has_been_deleted:
                    break
            else:
                yo._meta.current = -1
                return True
            return False
        finally:
            if not _move:
                yo._meta.current = current
    def bottom(yo, get_record=False):
        """sets record pointer to bottom of table
        if get_record, seeks to and returns last (non-deleted) record
        DbfError if table is empty
        Bof if all records deleted and use_deleted is False"""
        yo._meta.current = yo._meta.header.record_count
        if get_record:
            try:
                return yo.prev()
            except Bof:
                yo._meta.current = yo._meta.header.record_count
                raise Eof()
    def close(yo, keep_table=False, keep_memos=False):
        """closes disk files
        ensures table data is available if keep_table
        ensures memo data is available if keep_memos"""
        yo._meta.inmemory = True
        if keep_table:
            replacement_table = []
            for record in yo._table:
                replacement_table.append(record)
            yo._table = replacement_table
        else:
            if yo._meta.ondisk:
                yo._meta_only = True
        if yo._meta.mfd is not None:
            if not keep_memos:
                yo._meta.ignorememos = True
            else:
                memo_fields = []
                for field in yo.field_names:
                    if yo.is_memotype(field):
                        memo_fields.append(field)
                for record in yo:
                    for field in memo_fields:
                        record[field] = record[field]
            yo._meta.mfd.close()
            yo._meta.mfd = None
        if yo._meta.ondisk:
            yo._meta.dfd.close()
            yo._meta.dfd = None
        if keep_table:
            yo._read_only = True
        yo._meta.ondisk = False
    def create_backup(yo, new_name=None, overwrite=False):
        "creates a backup table -- ignored if memory table"
        if yo.filename[0] == yo.filename[-1] == ':':
            return
        if new_name is None:
            upper = yo.filename.isupper()
            name, ext = os.path.splitext(os.path.split(yo.filename)[1])
            extra = '_BACKUP' if upper else '_backup'
            new_name = os.path.join(temp_dir, name + extra + ext)
        else:
            overwrite = True
        if overwrite or not yo.backup:
            bkup = open(new_name, 'wb')
            try:
                yo._meta.dfd.seek(0)
                copyfileobj(yo._meta.dfd, bkup)
                yo.backup = new_name
            finally:
                bkup.close()
    def create_index(yo, key):
        return Index(yo, key)
    def current(yo, index=False):
        "returns current logical record, or its index"
        if yo._meta.current < 0:
            raise Bof()
        elif yo._meta.current >= yo._meta.header.record_count:
            raise Eof()
        if index:
            return yo._meta.current
        return yo._table[yo._meta.current]
    def delete_fields(yo, doomed):
        """removes field(s) from the table
        creates backup files with _backup appended to the file name,
        then modifies current structure"""
        doomed = yo._list_fields(doomed)
        for victim in doomed:
            if victim not in yo._meta.fields:
                raise DbfError("field %s not in table -- delete aborted" % victim)
        all_records = [record for record in yo]
        yo.create_backup()
        for victim in doomed:
            yo._meta.fields.pop(yo._meta.fields.index(victim))
            start = yo._meta[victim]['start']
            end = yo._meta[victim]['end']
            for record in yo:
                record._data = record._data[:start] + record._data[end:]
            for field in yo._meta.fields:
                if yo._meta[field]['start'] == end:
                    end = yo._meta[field]['end']
                    yo._meta[field]['start'] = start
                    yo._meta[field]['end'] = start + yo._meta[field]['length']
                    start = yo._meta[field]['end']
            yo._buildHeaderFields()
        yo._update_disk()
    def eof(yo, _move=False):
        "moves record pointer to next usable record; returns True if no more usable records"
        current = yo._meta.current
        try:
            while yo._meta.current < yo._meta.header.record_count - 1:
                yo._meta.current += 1
                if yo.use_deleted or not yo.current().has_been_deleted:
                    break
            else:
                yo._meta.current = yo._meta.header.record_count
                return True
            return False
        finally:
            if not _move:
                yo._meta.current = current
    def export(yo, records=None, filename=None, field_specs=None, format='csv', header=True):
        """writes the table using CSV or tab-delimited format, using the filename
        given if specified, otherwise the table name"""
        if filename is not None:
            path, filename = os.path.split(filename)
        else:
            path, filename = os.path.split(yo.filename)
        filename = os.path.join(path, filename)
        field_specs = yo._list_fields(field_specs)
        if records is None:
            records = yo
        format = format.lower()
        if format not in ('csv', 'tab', 'fixed'):
            raise DbfError("export format: csv, tab, or fixed -- not %s" % format)
        if format == 'fixed':
            format = 'txt'
        base, ext = os.path.splitext(filename)
        if ext.lower() in ('', '.dbf'):
            filename = base + "." + format[:3]
        fd = open(filename, 'w')
        try:
            if format == 'csv':
                csvfile = csv.writer(fd, dialect='dbf')
                if header:
                    csvfile.writerow(field_specs)
                for record in records:
                    fields = []
                    for fieldname in field_specs:
                        fields.append(record[fieldname])
                    csvfile.writerow(fields)
            elif format == 'tab':
                if header:
                    fd.write('\t'.join(field_specs) + '\n')
                for record in records:
                    fields = []
                    for fieldname in field_specs:
                        fields.append(str(record[fieldname]))
                    fd.write('\t'.join(fields) + '\n')
            else: # format == 'fixed'
                header = open("%s_layout.txt" % os.path.splitext(filename)[0], 'w')
                header.write("%-15s  Size\n" % "Field Name")
                header.write("%-15s  ----\n" % ("-" * 15))
                sizes = []
                for field in field_specs:
                    size = yo.size(field)[0]
                    sizes.append(size)
                    header.write("%-15s  %3d\n" % (field, size))
                header.write('\nTotal Records in file: %d\n' % len(records))
                header.close()
                for record in records:
                    fields = []
                    for i, field_name in enumerate(field_specs):
                        fields.append("%-*s" % (sizes[i], record[field_name]))
                    fd.write(''.join(fields) + '\n')
        finally:
            fd.close()
            fd = None
        return len(records)
    def find(yo, command):
        "uses exec to perform queries on the table"
        possible = List(desc="%s -->  %s" % (yo.filename, command), field_names=yo.field_names)
        yo._dbflists.add(possible)
        result = {}
        select = 'result["keep"] = %s' % command
        g = {}
        use_deleted = yo.use_deleted
        for record in yo:
            result['keep'] = False
            g['result'] = result
            exec select in g, record
            if result['keep']:
                possible.append(record)
            record.write_record()
        return possible
    def get_record(yo, recno):
        "returns record at physical_index[recno]"
        return yo._table[recno]
    def goto(yo, criteria):
        """changes the record pointer to the first matching (non-deleted) record
        criteria should be either a tuple of tuple(value, field, func) triples, 
        or an integer to go to"""
        if isinstance(criteria, int):
            if not -yo._meta.header.record_count <= criteria < yo._meta.header.record_count:
                raise IndexError("Record %d does not exist" % criteria)
            if criteria < 0:
                criteria += yo._meta.header.record_count
            yo._meta.current = criteria
            return yo.current()
        criteria = _normalize_tuples(tuples=criteria, length=3, filler=[_nop])
        specs = tuple([(field, func) for value, field, func in criteria])
        match = tuple([value for value, field, func in criteria])
        current = yo.current(index=True)
        matchlen = len(match)
        while not yo.Eof():
            record = yo.current()
            results = record(*specs)
            if results == match:
                return record
        return yo.goto(current)
    def is_decimal(yo, name):
        "returns True if name is a variable-length field type"
        return yo._meta[name]['type'] in yo._decimal_fields
    def is_memotype(yo, name):
        "returns True if name is a memo type field"
        return yo._meta[name]['type'] in yo._memotypes
    def new(yo, filename, field_specs=None, codepage=None):
        "returns a new table of the same type"
        if field_specs is None:
            field_specs = yo.structure()
        if not (filename[0] == filename[-1] == ':'):
            path, name = os.path.split(filename)
            if path == "":
                filename = os.path.join(os.path.split(yo.filename)[0], filename)
            elif name == "":
                filename = os.path.join(path, os.path.split(yo.filename)[1])
        if codepage is None:
            codepage = yo._meta.header.codepage()[0]
        return yo.__class__(filename, field_specs, codepage=codepage)
    def next(yo):
        "set record pointer to next (non-deleted) record, and return it"
        if yo.eof(_move=True):
            raise Eof()
        return yo.current()
    def open(yo):
        meta = yo._meta
        meta.inmemory = False
        meta.ondisk = True
        yo._read_only = False
        yo._meta_only = False
        if '_table' in dir(yo):
            del yo._table
        dfd = meta.dfd = open(meta.filename, 'r+b')
        dfd.seek(0)
        meta.header = header = yo._TableHeader(dfd.read(32))
        if not header.version in yo._supported_tables:
            dfd.close()
            dfd = None
            raise DbfError("Unsupported dbf type: %s [%x]" % (version_map.get(meta.header.version, 'Unknown: %s' % meta.header.version), ord(meta.header.version)))
        cp, sd, ld = _codepage_lookup(meta.header.codepage())
        meta.decoder = codecs.getdecoder(sd) 
        meta.encoder = codecs.getencoder(sd)
        fieldblock = dfd.read(header.start - 32)
        for i in range(len(fieldblock)//32+1):
            fieldend = i * 32
            if fieldblock[fieldend] == '\x0d':
                break
        else:
            raise DbfError("corrupt field structure in header")
        if len(fieldblock[:fieldend]) % 32 != 0:
            raise DbfError("corrupt field structure in header")
        header.fields = fieldblock[:fieldend]
        header.extra = fieldblock[fieldend+1:]  # skip trailing \r
        yo._initializeFields()
        yo._checkMemoIntegrity()
        meta.current = -1
        if len(yo) > 0:
            meta.current = 0
        dfd.seek(0)

    def pack(yo, _pack=True):
        "physically removes all deleted records"
        for dbfindex in yo._indexen:
            dbfindex.clear()
        newtable = []
        index = 0
        offset = 0 # +1 for each purged record
        for record in yo._table:
            found = False
            if record.has_been_deleted and _pack:
                for dbflist in yo._dbflists:
                    if dbflist._purge(record, record.record_number - offset, 1):
                        found = True
                record._recnum = -1
            else:
                record._recnum = index
                newtable.append(record)
                index += 1
            if found:
                offset += 1
                found = False
        yo._table.clear()
        for record in newtable:
            yo._table.append(record)
        yo._meta.header.record_count = index
        yo._current = -1
        yo._update_disk()
        yo.reindex()
    def prev(yo):
        "set record pointer to previous (non-deleted) record, and return it"
        if yo.bof(_move=True):
            raise Bof
        return yo.current()
    def query(yo, sql_command=None, python=None):
        "deprecated: use .find or .sql"
        if sql_command:
            return yo.sql(sql_command)
        elif python:
            return yo.find(python)
        raise DbfError("query: python parameter must be specified")
    def reindex(yo):
        for dbfindex in yo._indexen:
            dbfindex.reindex()
    def rename_field(yo, oldname, newname):
        "renames an existing field"
        if yo:
            yo.create_backup()
        if not oldname in yo._meta.fields:
            raise DbfError("field --%s-- does not exist -- cannot rename it." % oldname)
        if newname[0] == '_' or newname[0].isdigit() or not newname.replace('_','').isalnum():
            raise DbfError("field names cannot start with _ or digits, and can only contain the _, letters, and digits")
        newname = newname.lower()
        if newname in yo._meta.fields:
            raise DbfError("field --%s-- already exists" % newname)
        if len(newname) > 10:
            raise DbfError("maximum field name length is 10.  '%s' is %d characters long." % (newname, len(newname)))
        yo._meta[newname] = yo._meta[oldname]
        yo._meta.fields[yo._meta.fields.index(oldname)] = newname
        yo._buildHeaderFields()
        yo._update_disk(headeronly=True)
    def resize_field(yo, doomed, new_size):
        """resizes field (C only at this time)
        creates backup file, then modifies current structure"""
        if not 0 < new_size < 256:
            raise DbfError("new_size must be between 1 and 255 (use delete_fields to remove a field)")
        doomed = yo._list_fields(doomed)
        for victim in doomed:
            if victim not in yo._meta.fields:
                raise DbfError("field %s not in table -- resize aborted" % victim)
        all_records = [record for record in yo]
        yo.create_backup()
        #pprint(yo._meta['c_unit'])
        #print repr(yo[0].c_unit)
        for victim in doomed:
            delta = new_size - yo._meta[victim]['length']
            start = yo._meta[victim]['start']
            end = yo._meta[victim]['end']
            eff_end = min(yo._meta[victim]['length'], new_size)
            yo._meta[victim]['length'] = new_size
            yo._meta[victim]['end'] = start + new_size
            blank = array('c', ' ' * new_size)
            #print "\nstart=%s\nend=%s\neff_end=%s\nnew_size=%s\n\n" % (start, end, eff_end, new_size)
            for record in yo:
                new_data = blank[:]
                new_data[:eff_end] = record._data[start:start+eff_end]
                record._data = record._data[:start] + new_data + record._data[end:]
            for field in yo._meta.fields:
                if yo._meta[field]['start'] == end:
                    end = yo._meta[field]['end']
                    yo._meta[field]['start'] += delta
                    yo._meta[field]['end'] += delta #+ yo._meta[field]['length']
                    start = yo._meta[field]['end']
            #pprint(yo._meta['c_unit'])
            #print repr(yo[0].c_unit)
            #raw_input('...')
            yo._buildHeaderFields()
        yo._update_disk()
    def size(yo, field):
        "returns size of field as a tuple of (length, decimals)"
        if field in yo:
            return (yo._meta[field]['length'], yo._meta[field]['decimals'])
        raise DbfError("%s is not a field in %s" % (field, yo.filename))
    def sql(yo, command):
        "passes calls through to module level sql function"
        return sql(yo, command)
    def structure(yo, fields=None):
        """return list of fields suitable for creating same table layout
        @param fields: list of fields or None for all fields"""
        field_specs = []
        fields = yo._list_fields(fields)
        try:
            for name in fields:
                field_specs.append(yo._fieldLayout(yo.field_names.index(name)))
        except ValueError:
            raise DbfError("field --%s-- does not exist" % name)
        return field_specs
    def top(yo, get_record=False):
        """sets record pointer to top of table; if get_record, seeks to and returns first (non-deleted) record
        DbfError if table is empty
        Eof if all records are deleted and use_deleted is False"""
        yo._meta.current = -1
        if get_record:
            try:
                return yo.next()
            except Eof:
                yo._meta.current = -1
                raise Bof()
    def type(yo, field):
        "returns type of field"
        if field in yo:
            return yo._meta[field]['type']
        raise DbfError("%s is not a field in %s" % (field, yo.filename))
    def zap(yo, areyousure=False):
        """removes all records from table -- this cannot be undone!
        areyousure must be True, else error is raised"""
        if areyousure:
            if yo._meta.inmemory:
                yo._table = []
            else:
                yo._table.clear()
            yo._meta.header.record_count = 0
            yo._current = -1
            yo._update_disk()
        else:
            raise DbfError("You must say you are sure to wipe the table")
class Db3Table(DbfTable):
    """Provides an interface for working with dBase III tables."""
    _version = 'dBase III Plus'
    _versionabbv = 'db3'
    _fieldtypes = {
            'C' : {'Type':'Character', 'Retrieve':retrieveCharacter, 'Update':updateCharacter, 'Blank':str, 'Init':addCharacter, 'Class':None},
            'D' : {'Type':'Date', 'Retrieve':retrieveDate, 'Update':updateDate, 'Blank':Date.today, 'Init':addDate, 'Class':None},
            'L' : {'Type':'Logical', 'Retrieve':retrieveLogical, 'Update':updateLogical, 'Blank':bool, 'Init':addLogical, 'Class':None},
            'M' : {'Type':'Memo', 'Retrieve':retrieveMemo, 'Update':updateMemo, 'Blank':str, 'Init':addMemo, 'Class':None},
            'N' : {'Type':'Numeric', 'Retrieve':retrieveNumeric, 'Update':updateNumeric, 'Blank':int, 'Init':addNumeric, 'Class':None} }
    _memoext = '.dbt'
    _memotypes = ('M',)
    _memoClass = _Db3Memo
    _yesMemoMask = '\x80'
    _noMemoMask = '\x7f'
    _fixed_fields = ('D','L','M')
    _variable_fields = ('C','N')
    _character_fields = ('C','M') 
    _decimal_fields = ('N',)
    _numeric_fields = ('N',)
    _currency_fields = tuple()
    _dbfTableHeader = array('c', '\x00' * 32)
    _dbfTableHeader[0] = '\x03'         # version - dBase III w/o memo's
    _dbfTableHeader[8:10] = array('c', packShortInt(33))
    _dbfTableHeader[10] = '\x01'        # record length -- one for delete flag
    _dbfTableHeader[29] = '\x03'        # code page -- 437 US-MS DOS
    _dbfTableHeader = _dbfTableHeader.tostring()
    _dbfTableHeaderExtra = ''
    _supported_tables = ['\x03', '\x83']
    _read_only = False
    _meta_only = False
    _use_deleted = True
    def _checkMemoIntegrity(yo):
        "dBase III specific"
        if yo._meta.header.version == '\x83':
            try:
                yo._meta.memo = yo._memoClass(yo._meta)
            except:
                yo._meta.dfd.close()
                yo._meta.dfd = None
                raise
        if not yo._meta.ignorememos:
            for field in yo._meta.fields:
                if yo._meta[field]['type'] in yo._memotypes:
                    if yo._meta.header.version != '\x83':
                        yo._meta.dfd.close()
                        yo._meta.dfd = None
                        raise DbfError("Table structure corrupt:  memo fields exist, header declares no memos")
                    elif not os.path.exists(yo._meta.memoname):
                        yo._meta.dfd.close()
                        yo._meta.dfd = None
                        raise DbfError("Table structure corrupt:  memo fields exist without memo file")
                    break
    def _initializeFields(yo):
        "builds the FieldList of names, types, and descriptions"
        yo._meta.fields[:] = []
        offset = 1
        fieldsdef = yo._meta.header.fields
        if len(fieldsdef) % 32 != 0:
            raise DbfError("field definition block corrupt: %d bytes in size" % len(fieldsdef))
        if len(fieldsdef) // 32 != yo.field_count:
            raise DbfError("Header shows %d fields, but field definition block has %d fields" % (yo.field_count, len(fieldsdef)//32))
        for i in range(yo.field_count):
            fieldblock = fieldsdef[i*32:(i+1)*32]
            name = unpackStr(fieldblock[:11])
            type = fieldblock[11]
            if not type in yo._meta.fieldtypes:
                raise DbfError("Unknown field type: %s" % type)
            start = offset
            length = ord(fieldblock[16])
            offset += length
            end = start + length
            decimals = ord(fieldblock[17])
            flags = ord(fieldblock[18])
            yo._meta.fields.append(name)
            yo._meta[name] = {'type':type,'start':start,'length':length,'end':end,'decimals':decimals,'flags':flags}
class FpTable(DbfTable):
    'Provides an interface for working with FoxPro 2 tables'
    _version = 'Foxpro'
    _versionabbv = 'fp'
    _fieldtypes = {
            'C' : {'Type':'Character', 'Retrieve':retrieveCharacter, 'Update':updateCharacter, 'Blank':str, 'Init':addCharacter, 'Class':None},
            'F' : {'Type':'Float', 'Retrieve':retrieveNumeric, 'Update':updateNumeric, 'Blank':float, 'Init':addVfpNumeric, 'Class':None},
            'N' : {'Type':'Numeric', 'Retrieve':retrieveNumeric, 'Update':updateNumeric, 'Blank':int, 'Init':addVfpNumeric, 'Class':None},
            'L' : {'Type':'Logical', 'Retrieve':retrieveLogical, 'Update':updateLogical, 'Blank':bool, 'Init':addLogical, 'Class':None},
            'D' : {'Type':'Date', 'Retrieve':retrieveDate, 'Update':updateDate, 'Blank':Date.today, 'Init':addDate, 'Class':None},
            'M' : {'Type':'Memo', 'Retrieve':retrieveMemo, 'Update':updateMemo, 'Blank':str, 'Init':addVfpMemo, 'Class':None},
            'G' : {'Type':'General', 'Retrieve':retrieveMemo, 'Update':updateMemo, 'Blank':str, 'Init':addMemo, 'Class':None},
            'P' : {'Type':'Picture', 'Retrieve':retrieveMemo, 'Update':updateMemo, 'Blank':str, 'Init':addMemo, 'Class':None},
            '0' : {'Type':'_NullFlags', 'Retrieve':unsupportedType, 'Update':unsupportedType, 'Blank':int, 'Init':None, 'Class':None} }
    _memoext = '.fpt'
    _memotypes = ('G','M','P')
    _memoClass = _VfpMemo
    _yesMemoMask = '\xf5'               # 1111 0101
    _noMemoMask = '\x03'                # 0000 0011
    _fixed_fields = ('B','D','G','I','L','M','P','T','Y')
    _variable_fields = ('C','F','N')
    _character_fields = ('C','M')       # field representing character data
    _decimal_fields = ('F','N')
    _numeric_fields = ('F','N')
    _currency_fields = tuple()
    _supported_tables = ('\x03', '\xf5')
    _dbfTableHeader = array('c', '\x00' * 32)
    _dbfTableHeader[0] = '\x30'         # version - Foxpro 6  0011 0000
    _dbfTableHeader[8:10] = array('c', packShortInt(33+263))
    _dbfTableHeader[10] = '\x01'        # record length -- one for delete flag
    _dbfTableHeader[29] = '\x03'        # code page -- 437 US-MS DOS
    _dbfTableHeader = _dbfTableHeader.tostring()
    _dbfTableHeaderExtra = '\x00' * 263
    _use_deleted = True
    def _checkMemoIntegrity(yo):
        if os.path.exists(yo._meta.memoname):
            try:
                yo._meta.memo = yo._memoClass(yo._meta)
            except:
                yo._meta.dfd.close()
                yo._meta.dfd = None
                raise
        if not yo._meta.ignorememos:
            for field in yo._meta.fields:
                if yo._meta[field]['type'] in yo._memotypes:
                    if not os.path.exists(yo._meta.memoname):
                        yo._meta.dfd.close()
                        yo._meta.dfd = None
                        raise DbfError("Table structure corrupt:  memo fields exist without memo file")
                    break
    def _initializeFields(yo):
        "builds the FieldList of names, types, and descriptions"
        yo._meta.fields[:] = []
        offset = 1
        fieldsdef = yo._meta.header.fields
        if len(fieldsdef) % 32 != 0:
            raise DbfError("field definition block corrupt: %d bytes in size" % len(fieldsdef))
        if len(fieldsdef) // 32 != yo.field_count:
            raise DbfError("Header shows %d fields, but field definition block has %d fields" % (yo.field_count, len(fieldsdef)//32))
        for i in range(yo.field_count):
            fieldblock = fieldsdef[i*32:(i+1)*32]
            name = unpackStr(fieldblock[:11])
            type = fieldblock[11]
            if not type in yo._meta.fieldtypes:
                raise DbfError("Unknown field type: %s" % type)
            elif type == '0':
                return          # ignore nullflags
            start = offset
            length = ord(fieldblock[16])
            offset += length
            end = start + length
            decimals = ord(fieldblock[17])
            flags = ord(fieldblock[18])
            yo._meta.fields.append(name)
            yo._meta[name] = {'type':type,'start':start,'length':length,'end':end,'decimals':decimals,'flags':flags}
            
class VfpTable(DbfTable):
    'Provides an interface for working with Visual FoxPro 6 tables'
    _version = 'Visual Foxpro v6'
    _versionabbv = 'vfp'
    _fieldtypes = {
            'C' : {'Type':'Character', 'Retrieve':retrieveCharacter, 'Update':updateCharacter, 'Blank':str, 'Init':addCharacter, 'Class':None},
            'Y' : {'Type':'Currency', 'Retrieve':retrieveCurrency, 'Update':updateCurrency, 'Blank':Decimal(), 'Init':addVfpCurrency, 'Class':None},
            'B' : {'Type':'Double', 'Retrieve':retrieveDouble, 'Update':updateDouble, 'Blank':float, 'Init':addVfpDouble, 'Class':None},
            'F' : {'Type':'Float', 'Retrieve':retrieveNumeric, 'Update':updateNumeric, 'Blank':float, 'Init':addVfpNumeric, 'Class':None},
            'N' : {'Type':'Numeric', 'Retrieve':retrieveNumeric, 'Update':updateNumeric, 'Blank':int, 'Init':addVfpNumeric, 'Class':None},
            'I' : {'Type':'Integer', 'Retrieve':retrieveInteger, 'Update':updateInteger, 'Blank':int, 'Init':addVfpInteger, 'Class':None},
            'L' : {'Type':'Logical', 'Retrieve':retrieveLogical, 'Update':updateLogical, 'Blank':bool, 'Init':addLogical, 'Class':None},
            'D' : {'Type':'Date', 'Retrieve':retrieveDate, 'Update':updateDate, 'Blank':Date.today, 'Init':addDate, 'Class':None},
            'T' : {'Type':'DateTime', 'Retrieve':retrieveVfpDateTime, 'Update':updateVfpDateTime, 'Blank':DateTime.now, 'Init':addVfpDateTime, 'Class':None},
            'M' : {'Type':'Memo', 'Retrieve':retrieveVfpMemo, 'Update':updateVfpMemo, 'Blank':str, 'Init':addVfpMemo, 'Class':None},
            'G' : {'Type':'General', 'Retrieve':retrieveVfpMemo, 'Update':updateVfpMemo, 'Blank':str, 'Init':addVfpMemo, 'Class':None},
            'P' : {'Type':'Picture', 'Retrieve':retrieveVfpMemo, 'Update':updateVfpMemo, 'Blank':str, 'Init':addVfpMemo, 'Class':None},
            '0' : {'Type':'_NullFlags', 'Retrieve':unsupportedType, 'Update':unsupportedType, 'Blank':int, 'Init':None, 'Class':None} }
    _memoext = '.fpt'
    _memotypes = ('G','M','P')
    _memoClass = _VfpMemo
    _yesMemoMask = '\x30'               # 0011 0000
    _noMemoMask = '\x30'                # 0011 0000
    _fixed_fields = ('B','D','G','I','L','M','P','T','Y')
    _variable_fields = ('C','F','N')
    _character_fields = ('C','M')       # field representing character data
    _decimal_fields = ('F','N')
    _numeric_fields = ('B','F','I','N','Y')
    _currency_fields = ('Y',)
    _supported_tables = ('\x30',)
    _dbfTableHeader = array('c', '\x00' * 32)
    _dbfTableHeader[0] = '\x30'         # version - Foxpro 6  0011 0000
    _dbfTableHeader[8:10] = array('c', packShortInt(33+263))
    _dbfTableHeader[10] = '\x01'        # record length -- one for delete flag
    _dbfTableHeader[29] = '\x03'        # code page -- 437 US-MS DOS
    _dbfTableHeader = _dbfTableHeader.tostring()
    _dbfTableHeaderExtra = '\x00' * 263
    _use_deleted = True
    def _checkMemoIntegrity(yo):
        if os.path.exists(yo._meta.memoname):
            try:
                yo._meta.memo = yo._memoClass(yo._meta)
            except:
                yo._meta.dfd.close()
                yo._meta.dfd = None
                raise
        if not yo._meta.ignorememos:
            for field in yo._meta.fields:
                if yo._meta[field]['type'] in yo._memotypes:
                    if not os.path.exists(yo._meta.memoname):
                        yo._meta.dfd.close()
                        yo._meta.dfd = None
                        raise DbfError("Table structure corrupt:  memo fields exist without memo file")
                    break
    def _initializeFields(yo):
        "builds the FieldList of names, types, and descriptions"
        yo._meta.fields[:] = []
        offset = 1
        fieldsdef = yo._meta.header.fields
        for i in range(yo.field_count):
            fieldblock = fieldsdef[i*32:(i+1)*32]
            name = unpackStr(fieldblock[:11])
            type = fieldblock[11]
            if not type in yo._meta.fieldtypes:
                raise DbfError("Unknown field type: %s" % type)
            elif type == '0':
                return          # ignore nullflags
            start = unpackLongInt(fieldblock[12:16])
            length = ord(fieldblock[16])
            offset += length
            end = start + length
            decimals = ord(fieldblock[17])
            flags = ord(fieldblock[18])
            yo._meta.fields.append(name)
            yo._meta[name] = {'type':type,'start':start,'length':length,'end':end,'decimals':decimals,'flags':flags}
class List():
    "list of Dbf records, with set-like behavior"
    _desc = ''
    def __init__(yo, new_records=None, desc=None, key=None, field_names=None):
        yo.field_names = field_names
        yo._list = []
        yo._set = set()
        if key is not None:
            yo.key = key
            if key.__doc__ is None:
                key.__doc__ = 'unknown'
        key = yo.key
        yo._current = -1
        if isinstance(new_records, yo.__class__) and key is new_records.key:
                yo._list = new_records._list[:]
                yo._set = new_records._set.copy()
                yo._current = 0
        elif new_records is not None:
            for record in new_records:
                value = key(record)
                item = (record.record_table, record.record_number, value)
                if value not in yo._set:
                    yo._set.add(value)
                    yo._list.append(item)
            yo._current = 0
        if desc is not None:
            yo._desc = desc
    def __add__(yo, other):
        key = yo.key
        if isinstance(other, (DbfTable, list)):
            other = yo.__class__(other, key=key)
        if isinstance(other, yo.__class__):
            result = yo.__class__()
            result._set = yo._set.copy()
            result._list[:] = yo._list[:]
            result.key = yo.key
            if key is other.key:   # same key?  just compare key values
                for item in other._list:
                    if item[2] not in result._set:
                        result._set.add(item[2])
                        result._list.append(item)
            else:                   # different keys, use this list's key on other's records
                for rec in other:
                    value = key(rec)
                    if value not in result._set:
                        result._set.add(value)
                        result._list.append((rec.record_table, rec.record_number, value))
            result._current = 0 if result else -1
            return result
        return NotImplemented
    def __contains__(yo, record):
        if isinstance(record, tuple):
            item = record
        else:
            item = yo.key(record)
        return item in yo._set
    def __delitem__(yo, key):
        if isinstance(key, int):
            item = yo._list.pop[key]
            yo._set.remove(item[2])
        elif isinstance(key, slice):
            yo._set.difference_update([item[2] for item in yo._list[key]])
            yo._list.__delitem__(key)
        elif isinstance(key, _DbfRecord):
            index = yo.index(key)
            item = yo._list.pop[index]
            yo._set.remove(item[2])
        else:
            raise TypeError
    def __getitem__(yo, key):
        if isinstance(key, int):
            count = len(yo._list)
            if not -count <= key < count:
                raise IndexError("Record %d is not in list." % key)
            return yo._get_record(*yo._list[key])
        elif isinstance(key, slice):
            result = yo.__class__()
            result._list[:] = yo._list[key]
            result._set = set(result._list)
            result.key = yo.key
            result._current = 0 if result else -1
            return result
        elif isinstance(key, _DbfRecord):
            index = yo.index(key)
            return yo._get_record(*yo._list[index])
        else:
            raise TypeError('indices must be integers')
    def __iter__(yo):
        return (table.get_record(recno) for table, recno, value in yo._list)
    def __len__(yo):
        return len(yo._list)
    def __nonzero__(yo):
        return len(yo) > 0
    def __radd__(yo, other):
        return yo.__add__(other)
    def __repr__(yo):
        if yo._desc:
            return "%s(key=%s - %s - %d records)" % (yo.__class__, yo.key.__doc__, yo._desc, len(yo._list))
        else:
            return "%s(key=%s - %d records)" % (yo.__class__, yo.key.__doc__, len(yo._list))
    def __rsub__(yo, other):
        key = yo.key
        if isinstance(other, (DbfTable, list)):
            other = yo.__class__(other, key=key)
        if isinstance(other, yo.__class__):
            result = yo.__class__()
            result._list[:] = other._list[:]
            result._set = other._set.copy()
            result.key = key
            lost = set()
            if key is other.key:
                for item in yo._list:
                    if item[2] in result._list:
                        result._set.remove(item[2])
                        lost.add(item)
            else:
                for rec in other:
                    value = key(rec)
                    if value in result._set:
                        result._set.remove(value)
                        lost.add((rec.record_table, rec.record_number, value))
            result._list = [item for item in result._list if item not in lost]
            result._current = 0 if result else -1
            return result
        return NotImplemented
    def __sub__(yo, other):
        key = yo.key
        if isinstance(other, (DbfTable, list)):
            other = yo.__class__(other, key=key)
        if isinstance(other, yo.__class__):
            result = yo.__class__()
            result._list[:] = yo._list[:]
            result._set = yo._set.copy()
            result.key = key
            lost = set()
            if key is other.key:
                for item in other._list:
                    if item[2] in result._set:
                        result._set.remove(item[2])
                        lost.add(item[2])
            else:
                for rec in other:
                    value = key(rec)
                    if value in result._set:
                        result._set.remove(value)
                        lost.add(value)
            result._list = [item for item in result._list if item[2] not in lost]
            result._current = 0 if result else -1
            return result
        return NotImplemented
    def _maybe_add(yo, item):
        if item[2] not in yo._set:
            yo._set.add(item[2])
            yo._list.append(item)
    def _get_record(yo, table=None, rec_no=None, value=None):
        if table is rec_no is None:
            table, rec_no, value = yo._list[yo._current]
        return table.get_record(rec_no)
    def _purge(yo, record, old_record_number, offset):
        partial = record.record_table, old_record_number
        records = sorted(yo._list, key=lambda item: (item[0], item[1]))
        for item in records:
            if partial == item[:2]:
                found = True
                break
            elif partial[0] is item[0] and partial[1] < item[1]:
                found = False
                break
        else:
            found = False
        if found:
            yo._list.pop(yo._list.index(item))
            yo._set.remove(item[2])
        start = records.index(item) + found
        for item in records[start:]:
            if item[0] is not partial[0]:       # into other table's records
                break
            i = yo._list.index(item)
            yo._set.remove(item[2])
            item = item[0], (item[1] - offset), item[2]
            yo._list[i] = item
            yo._set.add(item[2])
        return found
    def append(yo, new_record):
        yo._maybe_add((new_record.record_table, new_record.record_number, yo.key(new_record)))
        if yo._current == -1 and yo._list:
            yo._current = 0
        #return new_record
    def bottom(yo):
        if yo._list:
            yo._current = len(yo._list) - 1
            return yo._get_record()
        raise DbfError("dbf.List is empty")
    def clear(yo):
        yo._list = []
        yo._set = set()
        yo._current = -1
    def current(yo):
        if yo._current < 0:
            raise Bof()
        elif yo._current == len(yo._list):
            raise Eof()
        return yo._get_record()
    def extend(yo, new_records):
        key = yo.key
        if isinstance(new_records, yo.__class__):
            if key is new_records.key:   # same key?  just compare key values
                for item in new_records._list:
                    yo._maybe_add(item)
            else:                   # different keys, use this list's key on other's records
                for rec in new_records:
                    value = key(rec)
                    yo._maybe_add((rec.record_table, rec.record_number, value))
        else:
            for record in new_records:
                value = key(rec)
                yo._maybe_add((rec.record_table, rec.record_number, value))
        if yo._current == -1 and yo._list:
            yo._current = 0
    def goto(yo, index_number):
        if yo._list:
            if 0 <= index_number <= len(yo._list):
                yo._current = index_number
                return yo._get_record()
            raise DbfError("index %d not in dbf.List of %d records" % (index_number, len(yo._list)))
        raise DbfError("dbf.List is empty")
    def index(yo, sort=None, reverse=False):
        "sort= ((field_name, func), (field_name, func),) | 'ORIGINAL'"
        if sort is None:
            results = []
            for field, func in yo._meta.index:
                results.append("%s(%s)" % (func.__name__, field))
            return ', '.join(results + ['reverse=%s' % yo._meta.index_reversed])
        yo._meta.index_reversed = reverse
        if sort == 'ORIGINAL':
            yo._index = range(yo._meta.header.record_count)
            yo._meta.index = 'ORIGINAL'
            if reverse:
                yo._index.reverse()
            return
        new_sort = _normalize_tuples(tuples=sort, length=2, filler=[_nop])
        yo._meta.index = tuple(new_sort)
        yo._meta.orderresults = [''] * len(yo)
        for record in yo:
            yo._meta.orderresults[record.record_number] = record()
        yo._index.sort(key=lambda i: yo._meta.orderresults[i], reverse=reverse)
    def index(yo, record, start=None, stop=None):
        item = record.record_table, record.record_number, yo.key(record)
        key = yo.key(record)
        if start is None:
            start = 0
        if stop is None:
            stop = len(yo._list)
        for i in range(start, stop):
            if yo._list[i][2] == key:
                return i
        else:
            raise ValueError("dbf.List.index(x): <x=%r> not in list" % (key,))
    def insert(yo, i, record):
        item = record.record_table, record.record_number, yo.key(record)
        if item not in yo._set:
            yo._set.add(item[2])
            yo._list.insert(i, item)
    def key(yo, record):
        "table_name, record_number"
        return record.record_table, record.record_number
    def next(yo):
        if yo._current < len(yo._list):
            yo._current += 1
            if yo._current < len(yo._list):
                return yo._get_record()
        raise Eof()
    def pop(yo, index=None):
        if index is None:
            table, recno, value = yo._list.pop()
        else:
            table, recno, value = yo._list.pop(index)
        yo._set.remove(value)
        return yo._get_record(table, recno, value)
    def prev(yo):
        if yo._current >= 0:
            yo._current -= 1
            if yo._current > -1:
                return yo._get_record()
        raise Bof()
    def remove(yo, record):
        item = record.record_table, record.record_number, yo.key(record)
        yo._list.remove(item)
        yo._set.remove(item[2])
    def reverse(yo):
        return yo._list.reverse()
    def top(yo):
        if yo._list:
            yo._current = 0
            return yo._get_record()
        raise DbfError("dbf.List is empty")
    def sort(yo, key=None, reverse=False):
        if key is None:
            return yo._list.sort(reverse=reverse)
        return yo._list.sort(key=lambda item: key(item[0].get_record(item[1])), reverse=reverse)

class Index():
    class IndexIterator():
        "returns records using this index"
        def __init__(yo, table, records):
            yo.table = table
            yo.records = records
            yo.index = 0
        def __iter__(yo):
            return yo
        def next(yo):
            while yo.index < len(yo.records):
                record = yo.table.get_record(yo.records[yo.index])
                yo.index += 1
                if not yo.table.use_deleted and record.has_been_deleted:
                    continue
                return record
            else:
                raise StopIteration
    def __init__(yo, table, key, field_names=None):
        yo._table = table
        yo._values = []             # ordered list of values
        yo._rec_by_val = []         # matching record numbers
        yo._records = {}            # record numbers:values
        yo.__doc__ = key.__doc__ or 'unknown'
        yo.key = key
        yo.field_names = field_names or table.field_names
        for record in table:
            value = key(record)
            if value is DoNotIndex:
                continue
            rec_num = record.record_number
            if not isinstance(value, tuple):
                value = (value, )
            vindex = bisect_right(yo._values, value)
            yo._values.insert(vindex, value)
            yo._rec_by_val.insert(vindex, rec_num)
            yo._records[rec_num] = value
        table._indexen.add(yo)
    def __call__(yo, record):
        rec_num = record.record_number
        if rec_num in yo._records:
            value = yo._records[rec_num]
            vindex = bisect_left(yo._values, value)
            yo._values.pop(vindex)
            yo._rec_by_val.pop(vindex)
        value = yo.key(record)
        if value is DoNotIndex:
            return
        if not isinstance(value, tuple):
            value = (value, )
        vindex = bisect_right(yo._values, value)
        yo._values.insert(vindex, value)
        yo._rec_by_val.insert(vindex, rec_num)
        yo._records[rec_num] = value
    def __contains__(yo, match):
        if isinstance(match, _DbfRecord):
            if match.record_table is yo._table:
                return match.record_number in yo._records
            match = yo.key(match)
        elif not isinstance(match, tuple):
            match = (match, )
        return yo.find(match) != -1
    def __getitem__(yo, key):
        if isinstance(key, int):
            count = len(yo._values)
            if not -count <= key < count:
                raise IndexError("Record %d is not in list." % key)
            rec_num = yo._rec_by_val[key]
            return yo._table.get_record(rec_num)
        elif isinstance(key, slice):
            result = List(field_names=yo._table.field_names)
            yo._table._dbflists.add(result)
            start, stop, step = key.start, key.stop, key.step
            if start is None: start = 0
            if stop is None: stop = len(yo._rec_by_val)
            if step is None: step = 1
            for loc in range(start, stop, step):
                record = yo._table.get_record(yo._rec_by_val[loc])
                result._maybe_add(item=(yo._table, yo._rec_by_val[loc], result.key(record)))
            result._current = 0 if result else -1
            return result
        elif isinstance (key, (str, unicode, tuple, _DbfRecord)):
            if isinstance(key, _DbfRecord):
                key = yo.key(key)
            elif not isinstance(key, tuple):
                key = (key, )
            loc = yo.find(key)
            if loc == -1:
                raise KeyError(key)
            return yo._table.get_record(yo._rec_by_val[loc])
        else:
            raise TypeError('indices must be integers, match objects must by strings or tuples')
    def __enter__(yo):
        return yo
    def __exit__(yo, *exc_info):
        yo._table.close()
        yo._values[:] = []
        yo._rec_by_val[:] = []
        yo._records.clear()
        return False
    def __iter__(yo):
        return yo.IndexIterator(yo._table, yo._rec_by_val)
    def __len__(yo):
        return len(yo._records)
    def _partial_match(yo, target, match):
        target = target[:len(match)]
        if isinstance(match[-1], (str, unicode)):
            target = list(target)
            target[-1] = target[-1][:len(match[-1])]
            target = tuple(target)
        return target == match
    def _purge(yo, rec_num):
        value = yo._records.get(rec_num)
        if value is not None:
            vindex = bisect_left(yo._values, value)
            del yo._records[rec_num]
            yo._values.pop(vindex)
            yo._rec_by_val.pop(vindex)
    def _search(yo, match, lo=0, hi=None):
        if hi is None:
            hi = len(yo._values)
        return bisect_left(yo._values, match, lo, hi)
    def clear(yo):
        "removes all entries from index"
        yo._values[:] = []
        yo._rec_by_val[:] = []
        yo._records.clear()
    def close(yo):
        yo._table.close()
    def find(yo, match, partial=False):
        "returns numeric index of (partial) match, or -1"
        if isinstance(match, _DbfRecord):
            if match.record_number in yo._records:
                return yo._values.index(yo._records[match.record_number])
            else:
                return -1
        if not isinstance(match, tuple):
            match = (match, )
        loc = yo._search(match)
        while loc < len(yo._values) and yo._values[loc] == match:
            if not yo._table.use_deleted and yo._table.get_record(yo._rec_by_val[loc]).has_been_deleted:
                loc += 1
                continue
            return loc
        if partial:
            while loc < len(yo._values) and yo._partial_match(yo._values[loc], match):
                if not yo._table.use_deleted and yo._table.get_record(yo._rec_by_val[loc]).has_been_deleted:
                    loc += 1
                    continue
                return loc
        return -1
    def find_index(yo, match):
        "returns numeric index of either (partial) match, or position of where match would be"
        if isinstance(match, _DbfRecord):
            if match.record_number in yo._records:
                return yo._values.index(yo._records[match.record_number])
            else:
                match = yo.key(match)
        if not isinstance(match, tuple):
            match = (match, )
        loc = yo._search(match)
        return loc
    def index(yo, match, partial=False):
        "returns numeric index of (partial) match, or raises ValueError"
        loc = yo.find(match, partial)
        if loc == -1:
            if isinstance(match, _DbfRecord):
                raise ValueError("table <%s> record [%d] not in index <%s>" % (yo._table.filename, match.record_number, yo.__doc__))
            else:
                raise ValueError("match criteria <%s> not in index" % (match, ))
        return loc
    def reindex(yo):
        "reindexes all records"
        for record in yo._table:
            yo(record)
    def query(yo, sql_command=None, python=None):
        """recognized sql commands are SELECT, UPDATE, REPLACE, INSERT, DELETE, and RECALL"""
        if sql_command:
            return sql(yo, sql_command)
        elif python is None:
            raise DbfError("query: python parameter must be specified")
        possible = List(desc="%s -->  %s" % (yo._table.filename, python), field_names=yo._table.field_names)
        yo._table._dbflists.add(possible)
        query_result = {}
        select = 'query_result["keep"] = %s' % python
        g = {}
        for record in yo:
            query_result['keep'] = False
            g['query_result'] = query_result
            exec select in g, record
            if query_result['keep']:
                possible.append(record)
            record.write_record()
        return possible
    def search(yo, match, partial=False):
        "returns dbf.List of all (partially) matching records"
        result = List(field_names=yo._table.field_names)
        yo._table._dbflists.add(result)
        if not isinstance(match, tuple):
            match = (match, )
        loc = yo._search(match)
        if loc == len(yo._values):
            return result
        while loc < len(yo._values) and yo._values[loc] == match:
            record = yo._table.get_record(yo._rec_by_val[loc])
            if not yo._table.use_deleted and record.has_been_deleted:
                loc += 1
                continue
            result._maybe_add(item=(yo._table, yo._rec_by_val[loc], result.key(record)))
            loc += 1
        if partial:
            while loc < len(yo._values) and yo._partial_match(yo._values[loc], match):
                record = yo._table.get_record(yo._rec_by_val[loc])
                if not yo._table.use_deleted and record.has_been_deleted:
                    loc += 1
                    continue
                result._maybe_add(item=(yo._table, yo._rec_by_val[loc], result.key(record)))
                loc += 1
        return result

# table meta
table_types = {
    'db3' : Db3Table,
    'fp'  : FpTable,
    'vfp' : VfpTable,
    'dbf' : DbfTable,
    }

version_map = {
        '\x02' : 'FoxBASE',
        '\x03' : 'dBase III Plus',
        '\x04' : 'dBase IV',
        '\x05' : 'dBase V',
        '\x30' : 'Visual FoxPro',
        '\x31' : 'Visual FoxPro (auto increment field)',
        '\x43' : 'dBase IV SQL',
        '\x7b' : 'dBase IV w/memos',
        '\x83' : 'dBase III Plus w/memos',
        '\x8b' : 'dBase IV w/memos',
        '\x8e' : 'dBase IV w/SQL table',
        '\xf5' : 'FoxPro w/memos'}

code_pages = {
        '\x00' : ('ascii', "plain ol' ascii"),
        '\x01' : ('cp437', 'U.S. MS-DOS'),
        '\x02' : ('cp850', 'International MS-DOS'),
        '\x03' : ('cp1252', 'Windows ANSI'),
        '\x04' : ('mac_roman', 'Standard Macintosh'),
        '\x08' : ('cp865', 'Danish OEM'),
        '\x09' : ('cp437', 'Dutch OEM'),
        '\x0A' : ('cp850', 'Dutch OEM (secondary)'),
        '\x0B' : ('cp437', 'Finnish OEM'),
        '\x0D' : ('cp437', 'French OEM'),
        '\x0E' : ('cp850', 'French OEM (secondary)'),
        '\x0F' : ('cp437', 'German OEM'),
        '\x10' : ('cp850', 'German OEM (secondary)'),
        '\x11' : ('cp437', 'Italian OEM'),
        '\x12' : ('cp850', 'Italian OEM (secondary)'),
        '\x13' : ('cp932', 'Japanese Shift-JIS'),
        '\x14' : ('cp850', 'Spanish OEM (secondary)'),
        '\x15' : ('cp437', 'Swedish OEM'),
        '\x16' : ('cp850', 'Swedish OEM (secondary)'),
        '\x17' : ('cp865', 'Norwegian OEM'),
        '\x18' : ('cp437', 'Spanish OEM'),
        '\x19' : ('cp437', 'English OEM (Britain)'),
        '\x1A' : ('cp850', 'English OEM (Britain) (secondary)'),
        '\x1B' : ('cp437', 'English OEM (U.S.)'),
        '\x1C' : ('cp863', 'French OEM (Canada)'),
        '\x1D' : ('cp850', 'French OEM (secondary)'),
        '\x1F' : ('cp852', 'Czech OEM'),
        '\x22' : ('cp852', 'Hungarian OEM'),
        '\x23' : ('cp852', 'Polish OEM'),
        '\x24' : ('cp860', 'Portugese OEM'),
        '\x25' : ('cp850', 'Potugese OEM (secondary)'),
        '\x26' : ('cp866', 'Russian OEM'),
        '\x37' : ('cp850', 'English OEM (U.S.) (secondary)'),
        '\x40' : ('cp852', 'Romanian OEM'),
        '\x4D' : ('cp936', 'Chinese GBK (PRC)'),
        '\x4E' : ('cp949', 'Korean (ANSI/OEM)'),
        '\x4F' : ('cp950', 'Chinese Big 5 (Taiwan)'),
        '\x50' : ('cp874', 'Thai (ANSI/OEM)'),
        '\x57' : ('cp1252', 'ANSI'),
        '\x58' : ('cp1252', 'Western European ANSI'),
        '\x59' : ('cp1252', 'Spanish ANSI'),
        '\x64' : ('cp852', 'Eastern European MS-DOS'),
        '\x65' : ('cp866', 'Russian MS-DOS'),
        '\x66' : ('cp865', 'Nordic MS-DOS'),
        '\x67' : ('cp861', 'Icelandic MS-DOS'),
        '\x68' : (None, 'Kamenicky (Czech) MS-DOS'),
        '\x69' : (None, 'Mazovia (Polish) MS-DOS'),
        '\x6a' : ('cp737', 'Greek MS-DOS (437G)'),
        '\x6b' : ('cp857', 'Turkish MS-DOS'),
        '\x78' : ('cp950', 'Traditional Chinese (Hong Kong SAR, Taiwan) Windows'),
        '\x79' : ('cp949', 'Korean Windows'),
        '\x7a' : ('cp936', 'Chinese Simplified (PRC, Singapore) Windows'),
        '\x7b' : ('cp932', 'Japanese Windows'),
        '\x7c' : ('cp874', 'Thai Windows'),
        '\x7d' : ('cp1255', 'Hebrew Windows'),
        '\x7e' : ('cp1256', 'Arabic Windows'),
        '\xc8' : ('cp1250', 'Eastern European Windows'),
        '\xc9' : ('cp1251', 'Russian Windows'),
        '\xca' : ('cp1254', 'Turkish Windows'),
        '\xcb' : ('cp1253', 'Greek Windows'),
        '\x96' : ('mac_cyrillic', 'Russian Macintosh'),
        '\x97' : ('mac_latin2', 'Macintosh EE'),
        '\x98' : ('mac_greek', 'Greek Macintosh') }

# SQL functions

def sql_select(records, chosen_fields, condition, field_names):
    if chosen_fields != '*':
        field_names = chosen_fields.replace(' ','').split(',')
    result = condition(records)
    result.modified = 0, 'record' + ('','s')[len(result)>1]
    result.field_names = field_names
    return result

def sql_update(records, command, condition, field_names):
    possible = condition(records)
    modified = sql_cmd(command, field_names)(possible)
    possible.modified = modified, 'record' + ('','s')[modified>1]
    return possible

def sql_delete(records, dead_fields, condition, field_names):
    deleted = condition(records)
    deleted.modified = len(deleted), 'record' + ('','s')[len(deleted)>1]
    deleted.field_names = field_names
    if dead_fields == '*':
        for record in deleted:
            record.delete_record()
            record.write_record()
    else:
        keep = [f for f in field_names if f not in dead_fields.replace(' ','').split(',')]
        for record in deleted:
            record.reset_record(keep_fields=keep)
            record.write_record()
    return deleted

def sql_recall(records, all_fields, condition, field_names):
    if all_fields != '*':
        raise DbfError('SQL RECALL: fields must be * (only able to recover at the record level)')
    revivified = List()
    tables = set()
    for record in records:
        tables.add(record_table)
    old_setting = dict()
    for table in tables:
        old_setting[table] = table.use_deleted
        table.use_deleted = True
    for record in condition(records):
        if record.has_been_deleted:
            revivified.append(record)
            record.undelete_record()
            record.write_record()
    for table in tables:
        table.use_deleted = old_setting[table]
    revivified.modfied = len(revivified), 'record' + ('','s')[len(revivified)>1]
    revivified.field_names = field_names
    return revivified

def sql_add(records, new_fields, condition, field_names):
    tables = set()
    possible = condition(records)
    for record in possible:
        tables.add(record.record_table)
    for table in tables:
        table.add_fields(new_fields)
    possible.modified = len(tables), 'table' + ('','s')[len(tables)>1]
    possible.field_names = field_names
    return possible

def sql_drop(records, dead_fields, condition, field_names):
    tables = set()
    possible = condition(records)
    for record in possible:
        tables.add(record.record_table)
    for table in tables:
        table.delete_fields(dead_fields)
    possible.modified = len(tables), 'table' + ('','s')[len(tables)>1]
    possible.field_names = field_names
    return possible

def sql_pack(records, command, condition, field_names):
    tables = set()
    possible = condition(records)
    for record in possible:
        tables.add(record.record_table)
    for table in tables:
        table.pack()
    possible.modified = len(tables), 'table' + ('','s')[len(tables)>1]
    possible.field_names = field_names
    return possible

def sql_resize(records, fieldname_newsize, condition, field_names):
    tables = set()
    possible = condition(records)
    for record in possible:
        tables.add(record.record_table)
    fieldname, newsize = fieldname_newsize.split()
    newsize = int(newsize)
    for table in tables:
        table.resize_field(fieldname, newsize)
    possible.modified = len(tables), 'table' + ('','s')[len(tables)>1]
    possible.field_names = field_names
    return possible

def sql_criteria(records, criteria):
    "creates a function matching the sql criteria"
    function = """def func(records):
    \"\"\"%s\"\"\"
    matched = List(field_names=records[0].field_names)
    for rec in records:
        %s

        if %s:
            matched.append(rec)
    return matched"""
    fields = []
    for field in records[0].field_names:
        if field in criteria:
            fields.append(field)
    fields = '\n        '.join(['%s = rec.%s' % (field, field) for field in fields])
    if 'record_number' in criteria:
        fields += '\n        record_number = rec.record_number'
    g = sql_user_functions.copy()
    g['List'] = List
    function %= (criteria, fields, criteria)
    #print function
    exec function in g
    return g['func']

def sql_cmd(command, field_names):
    "creates a function matching to apply command to each record in records"
    function = """def func(records):
    \"\"\"%s\"\"\"
    changed = 0
    for rec in records:
        %s

        %s

        %s
        changed += rec.write_record()
    return changed"""
    fields = []
    for field in field_names:
        if field in command:
            fields.append(field)
    pre_fields = '\n        '.join(['%s = rec.%s' % (field, field) for field in fields])
    post_fields = '\n        '.join(['rec.%s = %s' % (field, field) for field in fields])
    g = sql_user_functions.copy()
    if ' with ' in command.lower():
        offset = command.lower().index(' with ')
        command = command[:offset] + ' = ' + command[offset+6:]
    function %= (command, pre_fields, command, post_fields)
    #print function
    exec function in g
    return g['func']

def sql(records, command):
    """recognized sql commands are SELECT, UPDATE | REPLACE, DELETE, RECALL, ADD, DROP"""
    close_table = False
    if isinstance(records, (str, unicode)):
        records = Table(records)
        close_table = True
    try:
        sql_command = command
        if ' where ' in command:
            command, condition = command.split(' where ', 1)
            condition = sql_criteria(records, condition)
        else:
            def condition(records):
                return records[:]
        name, command = command.split(' ', 1)
        command = command.strip()
        name = name.lower()
        field_names = records[0].field_names
        if sql_functions.get(name) is None:
            raise DbfError('unknown SQL command: %s' % name.upper())
        result = sql_functions[name](records, command, condition, field_names)
        tables = set()
        for record in result:
            tables.add(record.record_table)
        for list_table in tables:
            list_table._dbflists.add(result)
    finally:
        if close_table:
            records.close()
    return result

sql_functions = {
        'select' : sql_select,
        'update' : sql_update,
        'replace': sql_update,
        'insert' : None,
        'delete' : sql_delete,
        'recall' : sql_recall,
        'add'    : sql_add,
        'drop'   : sql_drop,
        'count'  : None,
        'pack'   : sql_pack,
        'resize' : sql_resize,
        }


def _nop(value):
    "returns parameter unchanged"
    return value
def _normalize_tuples(tuples, length, filler):
    "ensures each tuple is the same length, using filler[-missing] for the gaps"
    final = []
    for t in tuples:
        if len(t) < length:
            final.append( tuple([item for item in t] + filler[len(t)-length:]) )
        else:
            final.append(t)
    return tuple(final)
def _codepage_lookup(cp):
    if cp not in code_pages:
        for code_page in sorted(code_pages.keys()):
            sd, ld = code_pages[code_page]
            if cp == sd or cp == ld:
                if sd is None:
                    raise DbfError("Unsupported codepage: %s" % ld)
                cp = code_page
                break
        else:
            raise DbfError("Unsupported codepage: %s" % cp)
    sd, ld = code_pages[cp]
    return cp, sd, ld
# miscellany

def ascii(new_setting=None):
    "get/set return_ascii setting"
    global return_ascii
    if new_setting is None:
        return return_ascii
    else:
        return_ascii = new_setting
def codepage(cp=None):
    "get/set default codepage for any new tables"
    global default_codepage
    cp, sd, ld = _codepage_lookup(cp or default_codepage)
    default_codepage = sd
    return "%s (LDID: 0x%02x - %s)" % (sd, ord(cp), ld)
def encoding(cp=None):
    "get/set default encoding for non-unicode strings passed into a table"
    global input_decoding
    cp, sd, ld = _codepage_lookup(cp or input_decoding)
    default_codepage = sd
    return "%s (LDID: 0x%02x - %s)" % (sd, ord(cp), ld)
class _Db4Table(DbfTable):
    version = 'dBase IV w/memos (non-functional)'
    _versionabbv = 'db4'
    _fieldtypes = {
            'C' : {'Type':'Character', 'Retrieve':retrieveCharacter, 'Update':updateCharacter, 'Blank':str, 'Init':addCharacter},
            'Y' : {'Type':'Currency', 'Retrieve':retrieveCurrency, 'Update':updateCurrency, 'Blank':Decimal(), 'Init':addVfpCurrency},
            'B' : {'Type':'Double', 'Retrieve':retrieveDouble, 'Update':updateDouble, 'Blank':float, 'Init':addVfpDouble},
            'F' : {'Type':'Float', 'Retrieve':retrieveNumeric, 'Update':updateNumeric, 'Blank':float, 'Init':addVfpNumeric},
            'N' : {'Type':'Numeric', 'Retrieve':retrieveNumeric, 'Update':updateNumeric, 'Blank':int, 'Init':addVfpNumeric},
            'I' : {'Type':'Integer', 'Retrieve':retrieveInteger, 'Update':updateInteger, 'Blank':int, 'Init':addVfpInteger},
            'L' : {'Type':'Logical', 'Retrieve':retrieveLogical, 'Update':updateLogical, 'Blank':bool, 'Init':addLogical},
            'D' : {'Type':'Date', 'Retrieve':retrieveDate, 'Update':updateDate, 'Blank':Date.today, 'Init':addDate},
            'T' : {'Type':'DateTime', 'Retrieve':retrieveVfpDateTime, 'Update':updateVfpDateTime, 'Blank':DateTime.now, 'Init':addVfpDateTime},
            'M' : {'Type':'Memo', 'Retrieve':retrieveMemo, 'Update':updateMemo, 'Blank':str, 'Init':addMemo},
            'G' : {'Type':'General', 'Retrieve':retrieveMemo, 'Update':updateMemo, 'Blank':str, 'Init':addMemo},
            'P' : {'Type':'Picture', 'Retrieve':retrieveMemo, 'Update':updateMemo, 'Blank':str, 'Init':addMemo},
            '0' : {'Type':'_NullFlags', 'Retrieve':unsupportedType, 'Update':unsupportedType, 'Blank':int, 'Init':None} }
    _memoext = '.dbt'
    _memotypes = ('G','M','P')
    _memoClass = _VfpMemo
    _yesMemoMask = '\x8b'               # 0011 0000
    _noMemoMask = '\x04'                # 0011 0000
    _fixed_fields = ('B','D','G','I','L','M','P','T','Y')
    _variable_fields = ('C','F','N')
    _character_fields = ('C','M')       # field representing character data
    _decimal_fields = ('F','N')
    _numeric_fields = ('B','F','I','N','Y')
    _currency_fields = ('Y',)
    _supported_tables = ('\x04', '\x8b')
    _dbfTableHeader = ['\x00'] * 32
    _dbfTableHeader[0] = '\x8b'         # version - Foxpro 6  0011 0000
    _dbfTableHeader[10] = '\x01'        # record length -- one for delete flag
    _dbfTableHeader[29] = '\x03'        # code page -- 437 US-MS DOS
    _dbfTableHeader = ''.join(_dbfTableHeader)
    _dbfTableHeaderExtra = ''
    _use_deleted = True
    def _checkMemoIntegrity(yo):
        "dBase III specific"
        if yo._meta.header.version == '\x8b':
            try:
                yo._meta.memo = yo._memoClass(yo._meta)
            except:
                yo._meta.dfd.close()
                yo._meta.dfd = None
                raise
        if not yo._meta.ignorememos:
            for field in yo._meta.fields:
                if yo._meta[field]['type'] in yo._memotypes:
                    if yo._meta.header.version != '\x8b':
                        yo._meta.dfd.close()
                        yo._meta.dfd = None
                        raise DbfError("Table structure corrupt:  memo fields exist, header declares no memos")
                    elif not os.path.exists(yo._meta.memoname):
                        yo._meta.dfd.close()
                        yo._meta.dfd = None
                        raise DbfError("Table structure corrupt:  memo fields exist without memo file")
                    break

# utility functions

def Table(
        filename, 
        field_specs='', 
        memo_size=128, 
        ignore_memos=False,
        read_only=False, 
        keep_memos=False, 
        meta_only=False, 
        dbf_type=None, 
        codepage=None,
        numbers='default',
        strings=str,
        currency=Decimal,
        ):
    "returns an open table of the correct dbf_type, or creates it if field_specs is given"
    if dbf_type is None and isinstance(filename, DbfTable):
        return filename
    if field_specs and dbf_type is None:
        dbf_type = default_type
    if dbf_type is not None:
        dbf_type = dbf_type.lower()
        table = table_types.get(dbf_type)
        if table is None:
            raise DbfError("Unknown table type: %s" % dbf_type)
        return table(filename, field_specs, memo_size, ignore_memos, read_only, keep_memos, meta_only, codepage, numbers, strings, currency)
    else:
        possibles = guess_table_type(filename)
        if len(possibles) == 1:
            return possibles[0][2](filename, field_specs, memo_size, ignore_memos, \
                                 read_only, keep_memos, meta_only, codepage, numbers, strings, currency)
        else:
            for type, desc, cls in possibles:
                if type == default_type:
                    return cls(filename, field_specs, memo_size, ignore_memos, \
                                 read_only, keep_memos, meta_only, codepage, numbers, strings, currency)
            else:
                types = ', '.join(["%s" % item[1] for item in possibles])
                abbrs = '[' + ' | '.join(["%s" % item[0] for item in possibles]) + ']'
                raise DbfError("Table could be any of %s.  Please specify %s when opening" % (types, abbrs))
def index(sequence):
    "returns integers 0 - len(sequence)"
    for i in xrange(len(sequence)):
        yield i    
def guess_table_type(filename):
    reported = table_type(filename)
    possibles = []
    version = reported[0]
    for tabletype in (Db3Table, FpTable, VfpTable):
        if version in tabletype._supported_tables:
            possibles.append((tabletype._versionabbv, tabletype._version, tabletype))
    if not possibles:
        raise DbfError("Tables of type %s not supported" % str(reported))
    return possibles
def table_type(filename):
    "returns text representation of a table's dbf version"
    base, ext = os.path.splitext(filename)
    if ext == '':
        filename = base + '.dbf'
    if not os.path.exists(filename):
        raise DbfError('File %s not found' % filename)
    fd = open(filename)
    version = fd.read(1)
    fd.close()
    fd = None
    if not version in version_map:
        raise DbfError("Unknown dbf type: %s (%x)" % (version, ord(version)))
    return version, version_map[version]

def add_fields(table_name, field_specs):
    "adds fields to an existing table"
    table = Table(table_name)
    try:
        table.add_fields(field_specs)
    finally:
        table.close()
def delete_fields(table_name, field_names):
    "deletes fields from an existing table"
    table = Table(table_name)
    try:
        table.delete_fields(field_names)
    finally:
        table.close()
def export(table_name, filename='', fields='', format='csv', header=True):
    "creates a csv or tab-delimited file from an existing table"
    if fields is None:
        fields = []
    table = Table(table_name)
    try:
        table.export(filename=filename, field_specs=fields, format=format, header=header)
    finally:
        table.close()
def first_record(table_name):
    "prints the first record of a table"
    table = Table(table_name)
    try:
        print str(table[0])
    finally:
        table.close()
def from_csv(csvfile, to_disk=False, filename=None, field_names=None, extra_fields=None, dbf_type='db3', memo_size=64, min_field_size=1):
    """creates a Character table from a csv file
    to_disk will create a table with the same name
    filename will be used if provided
    field_names default to f0, f1, f2, etc, unless specified (list)
    extra_fields can be used to add additional fields -- should be normal field specifiers (list)"""
    reader = csv.reader(open(csvfile))
    if field_names:
        field_names = ['%s M' % fn for fn in field_names]
    else:
        field_names = ['f0 M']
    mtable = Table(':memory:', [field_names[0]], dbf_type=dbf_type, memo_size=memo_size)
    fields_so_far = 1
    for row in reader:
        while fields_so_far < len(row):
            if fields_so_far == len(field_names):
                field_names.append('f%d M' % fields_so_far)
            mtable.add_fields(field_names[fields_so_far])
            fields_so_far += 1
        mtable.append(tuple(row))
    if filename:
        to_disk = True
    if not to_disk:
        if extra_fields:
            mtable.add_fields(extra_fields)
    else:
        if not filename:
            filename = os.path.splitext(csvfile)[0]
        length = [min_field_size] * len(field_names)
        for record in mtable:
            for i in index(record.field_names):
                length[i] = max(length[i], len(record[i]))
        fields = mtable.field_names
        fielddef = []
        for i in index(length):
            if length[i] < 255:
                fielddef.append('%s C(%d)' % (fields[i], length[i]))
            else:
                fielddef.append('%s M' % (fields[i]))
        if extra_fields:
            fielddef.extend(extra_fields)
        csvtable = Table(filename, fielddef, dbf_type=dbf_type)
        for record in mtable:
            csvtable.append(record.scatter_fields())
        return csvtable
    return mtable
def get_fields(table_name):
    "returns the list of field names of a table"
    table = Table(table_name)
    return table.field_names
def info(table_name):
    "prints table info"
    table = Table(table_name)
    print str(table)
def rename_field(table_name, oldfield, newfield):
    "renames a field in a table"
    table = Table(table_name)
    try:
        table.rename_field(oldfield, newfield)
    finally:
        table.close()
def structure(table_name, field=None):
    "returns the definition of a field (or all fields)"
    table = Table(table_name)
    return table.structure(field)
def hex_dump(records):
    "just what it says ;)"
    for index,dummy in enumerate(records):
        chars = dummy._data
        print "%2d: " % index,
        for char in chars[1:]:
            print " %2x " % ord(char),
        print
