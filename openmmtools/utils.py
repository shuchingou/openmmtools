#!/usr/bin/env python

# =============================================================================
# MODULE DOCSTRING
# =============================================================================

"""
General utility functions for the repo.

"""


# =============================================================================
# GLOBAL IMPORTS
# =============================================================================

import re
import ast
import abc
import time
import math
import copy
import logging
import operator
import importlib

import numpy as np
from simtk import openmm, unit

logger = logging.getLogger(__name__)


# =============================================================================
# BENCHMARKING UTILITIES
# =============================================================================

class Timer(object):
    """A class with stopwatch-style timing functions.

    Examples
    --------
    >>> timer = Timer()
    >>> timer.start('my benchmark')
    >>> for i in range(10):
    ...     pass
    >>> timer.stop('my benchmark')
    >>> timer.start('second benchmark')
    >>> for i in range(10):
    ...     for j in range(10):
    ...         pass
    >>> timer.start('second benchmark')
    >>> timer.report_timing()

    """

    def __init__(self):
        self.reset_timing_statistics()

    def reset_timing_statistics(self):
        """Reset the timing statistics."""
        self._t0 = {}
        self._t1 = {}
        self._elapsed = {}

    def start(self, benchmark_id):
        """Start a timer with given benchmark_id."""
        self._t0[benchmark_id] = time.time()

    def stop(self, benchmark_id):
        if benchmark_id in self._t0:
            self._t1[benchmark_id] = time.time()
            self._elapsed[benchmark_id] = self._t1[benchmark_id] - self._t0[benchmark_id]
        else:
            logger.info("Can't stop timing for {}".format(benchmark_id))

    def report_timing(self, clear=True):
        """Log all the timings at the debug level.

        Parameters
        ----------
        clear : bool
            If True, the stored timings are deleted after being reported.

        """
        logger.debug('Saved timings:')

        for benchmark_id, elapsed_time in self._elapsed.items():
            logger.debug('{:.24}: {:8.3f}s'.format(benchmark_id, elapsed_time))

        if clear is True:
            self.reset_timing_statistics()


# =============================================================================
# STRING MATHEMATICAL EXPRESSION PARSING UTILITIES
# =============================================================================

# Dict reserved_keyword: compiled_regex_pattern. This is used by
_RESERVED_WORDS_PATTERNS = {
    'lambda': re.compile(r'(?<![a-zA-Z0-9_])lambda(?![a-zA-Z0-9_])')
}


def sanitize_expression(expression, variables):
    """Sanitize variables with an illegal Python name.

    Transform variable names in the string expression that are illegal in
    Python so that the expression can be evaluated in pure Python. Currently
    this just handle variables called with the reserved word 'lambda'.

    Parameters
    ----------
    expression : str
        The mathematical expression as a string.
    variables : dict of str: float
        The variables in the expression.

    Returns
    -------
    sanitized_expression : str
        The same mathematical expression that can be executed in Python.
    sanitized_variables : dict of str: float
        The updated variable names with their values.

    """
    sanitized_variables = None
    sanitized_expression = expression

    # Substitute all reserved words in expression and variables.
    for word, pattern in _RESERVED_WORDS_PATTERNS.items():
        if word in variables:  # Don't make unneeded substitutions.
            if sanitized_variables is None:
                sanitized_variables = copy.deepcopy(variables)
            sanitized_word = '_sanitized__' + word
            sanitized_expression = pattern.sub(sanitized_word, sanitized_expression)
            variable_value = sanitized_variables.pop(word)
            sanitized_variables[sanitized_word] = variable_value

    # If no substitutions are made return same variables.
    if sanitized_variables is None:
        sanitized_variables = variables

    return sanitized_expression, sanitized_variables


def math_eval(expression, variables=None, functions=None):
    """Evaluate a mathematical expression with variables.

    All the functions in the standard module math are available together with
    - step(x) : Heaviside step function (1.0 for x=0)
    - step_hm(x) : Heaviside step function with half-maximum convention.
    - sign(x) : sign function (0.0 for x=0.0)

    Parameters
    ----------
    expression : str
        The mathematical expression as a string.
    variables : dict of str: float, optional
        The variables in the expression, if any (default is None).
    functions : dict of str: callable function, optional
        Additional functions to teach the math eval statement how to handle.
        Built-in functions are 'step', 'step_hm', and 'sign'

    Returns
    -------
    float
        The result of the evaluated expression.

    Examples
    --------
    >>> expr = '-((x + ceil(y)) / z * 4 + step(-0.2))**2'
    >>> vars = {'x': 1, 'y': 1.9, 'z': 3}
    >>> math_eval(expr, vars)
    -16.0

    """
    # Supported operators.
    operators = {ast.Add: operator.add, ast.Sub: operator.sub,
                 ast.Mult: operator.mul, ast.Div: operator.truediv,
                 ast.Pow: operator.pow, ast.USub: operator.neg}

    # Supported functions, not defined in math.
    if functions is None:
        functions = {}
    functions.update({'step': lambda x: 1 * (x >= 0),
                      'step_hm': lambda x: 0.5 * (np.sign(x) + 1),
                      'sign': lambda x: np.sign(x)}
                     )

    def _math_eval(node):
        if isinstance(node, ast.Num):
            return node.n
        elif isinstance(node, ast.UnaryOp):
            return operators[type(node.op)](_math_eval(node.operand))
        elif isinstance(node, ast.BinOp):
            return operators[type(node.op)](_math_eval(node.left),
                                            _math_eval(node.right))
        elif isinstance(node, ast.Name):
            try:
                return variables[node.id]
            except KeyError:
                raise ValueError('Variable {} was not provided'.format(node.id))
        elif isinstance(node, ast.Call):
            args = [_math_eval(arg) for arg in node.args]
            try:
                return getattr(math, node.func.id)(*args)
            except AttributeError:
                try:
                    return functions[node.func.id](*args)
                except KeyError:
                    raise ValueError('Function {} is not supported'.format(node.func.id))
        else:
            raise TypeError('Cannot parse expression: {}'.format(expression))

    if variables is None:
        variables = {}

    # Sanitized reserved words.
    expression, variables = sanitize_expression(expression, variables)

    return _math_eval(ast.parse(expression, mode='eval').body)


# =============================================================================
# QUANTITY UTILITIES
# =============================================================================

# List of simtk.unit methods that are actually units and functions instead of base classes
# Pre-computed to reduce run-time cost
# Get the built-in units
_VALID_UNITS = {method: getattr(unit, method) for method in dir(unit) if type(getattr(unit, method)) is unit.Unit}
# Get the built in unit functions and make sure they are not just types
_VALID_UNIT_FUNCTIONS = {method: getattr(unit, method) for method in dir(unit)
                         if callable(getattr(unit, method)) and type(getattr(unit, method)) is not type}


def is_quantity_close(quantity1, quantity2):
    """Check if the quantities are equal up to floating-point precision errors.

    Parameters
    ----------
    quantity1 : simtk.unit.Quantity
        The first quantity to compare.
    quantity2 : simtk.unit.Quantity
        The second quantity to compare.

    Returns
    -------
    True if the quantities are equal up to approximately 10 digits.

    Raises
    ------
    TypeError
        If the two quantities are of incompatible units.

    """
    if not quantity1.unit.is_compatible(quantity2.unit):
        raise TypeError('Cannot compare incompatible quantities {} and {}'.format(
            quantity1, quantity2))

    value1 = quantity1.value_in_unit_system(unit.md_unit_system)
    value2 = quantity2.value_in_unit_system(unit.md_unit_system)

    # np.isclose is not symmetric, so we make it so.
    if value2 >= value1:
        return np.isclose(value1, value2, rtol=1e-10, atol=0.0)
    else:
        return np.isclose(value2, value1, rtol=1e-10, atol=0.0)


def quantity_from_string(expression):
    """Special call to the math_eval function designed to handle simtk.unit Quantity strings

    All the functions in the standard module math are available together with
    most of the methods inside the simtk.unit module.

    Parameters
    ----------
    expression : str
        The mathematical expression to rebuild a Quantityas a string.

    Returns
    -------
    Quantity
        The result of the evaluated expression.

    Examples
    --------
    >>> expr = '4 * kilojoules / mole'
    >>> quantity_from_string(expr)
    Quantity(value=4.000000000000002, unit=kilojoule/mole)

    """

    # Supported functions, not defined in math.
    functions = _VALID_UNIT_FUNCTIONS

    # Define the units from simtk.unit as the variables
    variables = _VALID_UNITS

    # Eliminate nested quotes and excess whitespace
    expression = expression.strip('\'" ')

    # Handle a special case of the unit when it is just "inverse unit", e.g. Hz == /second
    if expression[0] == '/':
        expression = '(' + expression[1:] + ')**(-1)'

    return math_eval(expression, variables=variables, functions=functions)


def typename(atype):
    """Convert a type object into a fully qualified typename.

    Parameters
    ----------
    atype : type
        The type to convert

    Returns
    -------
    typename : str
        The string typename.

    For example,

    >>> typename(type(1))
    'int'

    >>> import numpy
    >>> x = numpy.array([1,2,3], numpy.float32)
    >>> typename(type(x))
    'numpy.ndarray'

    """
    if not isinstance(atype, type):
        raise Exception('Argument is not a type')

    modulename = atype.__module__
    typename = atype.__name__

    if modulename != '__builtin__':
        typename = modulename + '.' + typename

    return typename


# =============================================================================
# OPENMM PLATFORM UTILITIES
# =============================================================================

def get_available_platforms():
    """Return a list of the available OpenMM Platforms."""
    return [openmm.Platform.getPlatform(i)
            for i in range(openmm.Platform.getNumPlatforms())]


def get_fastest_platform():
    """Return the fastest available platform.

    This relies on the hardcoded speed values in Platform.getSpeed().

    Returns
    -------
    platform : simtk.openmm.Platform
       The fastest available platform.

    """
    platforms = get_available_platforms()
    fastest_platform = max(platforms, key=lambda x: x.getSpeed())
    return fastest_platform


# =============================================================================
# SERIALIZATION UTILITIES
# =============================================================================

_SERIALIZED_MANGLED_PREFIX = '_serialized__'


def serialize(instance):
    """Serialize an object.

    The object must expose a __getstate__ method that returns a
    dictionary representation of its state. This will be passed to
    __setstate__ for deserialization. The function automatically
    handle the resolution of the correct class.

    Parameters
    ----------
    instance : object
        An instance of a new style class.

    Returns
    -------
    serialization : dict
        A dictionary representation of the object that can be
        stored in several formats (e.g. JSON, YAML, HDF5) and
        reconstructed into the original object with deserialize().

    """
    module_name = instance.__module__
    class_name = instance.__class__.__name__
    try:
        serialization = instance.__getstate__()
    except AttributeError:
        raise ValueError('Cannot serialize class {} without a __getstate__ method'.format(class_name))
    serialization[_SERIALIZED_MANGLED_PREFIX + 'module_name'] = module_name
    serialization[_SERIALIZED_MANGLED_PREFIX + 'class_name'] = class_name
    return serialization


def deserialize(serialization):
    """Deserialize an object.

    The original class must expose a __setstate__ that takes the
    dictionary representation of its state generated by its
    __getstate__.

    Parameters
    ----------
    serialization : dict
        A dictionary generated by serialize().

    Returns
    -------
    instance : object
        An instance in the state given by serialization.

    """
    names = []
    for key in ['module_name', 'class_name']:
        try:
            names.append(serialization.pop(_SERIALIZED_MANGLED_PREFIX + key))
        except KeyError:
            raise ValueError('Cannot find {} in the serialization. Was the original object '
                             'serialized with openmmtools.utils.serialize()?'.format(key))
    module_name, class_name = names  # unpack
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    instance = object.__new__(cls)
    try:
        instance.__setstate__(serialization)
    except AttributeError:
        raise ValueError('Cannot deserialize class {} without a __setstate__ method'.format(class_name))
    return instance


# =============================================================================
# METACLASS UTILITIES
# =============================================================================

# TODO Remove this when we drop Python 2 support.
def with_metaclass(metaclass, *bases):
    """Create a base class with a metaclass.

    Imported from six (MIT license): https://pypi.python.org/pypi/six.
    Provide a Python2/3 compatible way to create a metaclass.

    """
    # This requires a bit of explanation: the basic idea is to make a dummy
    # metaclass for one level of class instantiation that replaces itself with
    # the actual metaclass.
    class Metaclass(metaclass):
        def __new__(cls, name, this_bases, d):
            return metaclass(name, bases, d)
    return type.__new__(Metaclass, 'temporary_class', (), {})


class SubhookedABCMeta(with_metaclass(abc.ABCMeta)):
    """Abstract class with an implementation of __subclasshook__.

    The __subclasshook__ method checks that the instance implement the
    abstract properties and methods defined by the abstract class. This
    allow classes to implement an abstraction without explicitly
    subclassing it.

    Examples
    --------
    >>> class MyInterface(SubhookedABCMeta):
    ...     @abc.abstractmethod
    ...     def my_method(self): pass
    >>> class Implementation(object):
    ...     def my_method(self): return True
    >>> isinstance(Implementation(), MyInterface)
    True

    """
    @classmethod
    def __subclasshook__(cls, subclass):
        for abstract_method in cls.__abstractmethods__:
            if not any(abstract_method in C.__dict__ for C in subclass.__mro__):
                return False
        return True


if __name__ == '__main__':
    import doctest
    doctest.testmod()
