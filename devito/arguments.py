import abc

import numpy as np
from collections import OrderedDict
from sympy import Symbol
from cached_property import cached_property
from collections import defaultdict, namedtuple, OrderedDict
from functools import reduce
from itertools import chain

from devito.exceptions import InvalidArgument
from devito.logger import debug, error
from devito.tools import filter_ordered, flatten, GenericVisitor
from devito.function import CompositeFunction, SymbolicFunction
from devito.dimension import Dimension
from devito.ir.support.stencil import retrieve_offsets


""" This module contains a set of classes and functions to deal with runtime arguments
to Operators. It represents the arguments and their relationships as a DAG (N, E) where
every node (N) is represented by an object of class Parameter and every edge is an object
of class Dependency. 
The various class hierarchies are explained here:
Parameter:
"""

class Parameter(object):
    """ Abstract base class for any object that represents a node in the dependency
        graph. It may or may not represent a runtime argument. 
    """
    is_Argument = False
    is_ScalarArgument = False
    is_TensorArgument = False
    is_PtrArgument = False
    
    __metaclass__ = abc.ABCMeta
    def __init__(self, name, dependencies):
        self.name = name
        self.dependencies = dependencies

    @property
    def gets_value_from(self):
        return [x for x in self.dependencies if x.dependency_type=="gets_value_from"]

    @property
    def verified_by(self):
        return [x for x in self.dependencies if x.dependency_type=="verified_by"]

    def __repr__(self):
        return self.name + ", Depends: " + str(self.dependencies)


class DimensionParameter(Parameter):
    """ Parameter object (node in the dependency graph) that represents a Dimension.
        A dimension object plays an important role in value derivation and verification
        but does not represent a runtime argument itself (since it provides multiple 
        ScalarArguments). 
    """
    def __init__(self, provider, dependencies):
        super(DimensionParameter, self).__init__(provider.name, dependencies)
        self.provider = provider

    
class Argument(Parameter):

    """ Base class for any object that represents a run time argument for
        generated kernels. It is necessarily a node in the dependency graph. 
    """
    is_Argument = True


class ScalarArgument(Argument):

    """ Class representing scalar arguments that a kernel might expect.
        Most commonly used to pass dimension sizes
        enforce determines whether any reduction will be performed or not.
        i.e. if it is a user-provided value, use it directly.
    """

    is_ScalarArgument = True

    def __init__(self, name, dependencies, dtype=np.int32):
        super(ScalarArgument, self).__init__(name, dependencies)
        self.dtype = dtype


class TensorArgument(Argument):

    """ Class representing tensor arguments that a kernel might expect.
        Most commonly used to pass numpy-like multi-dimensional arrays.
    """

    is_TensorArgument = True

    def __init__(self, provider, dependencies=[]):
        super(TensorArgument, self).__init__(provider.name, dependencies + [Dependency("gets_value_from", provider)])
        self.dtype = provider.dtype
        self.provider = provider


class PtrArgument(Argument):

    """ Class representing arbitrary arguments that a kernel might expect.
        These are passed as void pointers and then promptly casted to their
        actual type.
    """

    is_PtrArgument = True

    def __init__(self, provider):
        super(PtrArgument, self).__init__(provider.name, [Dependency("gets_value_from", provider)])
        self.dtype = provider.dtype


class ArgumentEngine(object):
    """ Class that encapsulates the argument derivation and verification subsystem
    """
    def __init__(self, stencils, parameters, dle_arguments):
        self.stencils = stencils
        self.parameters = parameters
        self.dle_arguments = dle_arguments
        self.argument_mapper = self._build_argument_mapper(parameters)
        self.arguments = filter_ordered([x for x in self.argument_mapper if isinstance(x, Argument)], key=lambda x: x.name)
        self.dims = [x for x in self.argument_mapper if isinstance(x, DimensionParameter)]
        self.offsets = {d.end_name: v for d, v in retrieve_offsets(stencils).items()}

    def handle(self, **kwargs):

        user_autotune = kwargs.pop('autotune', False)

        kwargs = self._offset_adjust(kwargs)
        
        kwargs = self._extract_children_of_composites(kwargs)

        values = self._derive_values(kwargs)

        # The following is only being done to update the autotune flag. The actual value derivation for the
        # dle arguments has moved inside the above _derive_values method.
        # TODO: Refactor so this is not required
        dim_sizes = dict([(d.name, runtime_dim_extent(d, values)) for d in self.dimensions])
        dle_arguments, dle_autotune = self._dle_arguments(dim_sizes)
        

        assert(self._verify(values))

        arguments = OrderedDict([(k.name, v) for k, v in values.items()])
        return arguments, user_autotune and dle_autotune

    def _offset_adjust(self, kwargs):
        for k, v in kwargs.items():
            if k in self.offsets:
                kwargs[k] = v + self.offsets[k]
        return kwargs

    def _build_argument_mapper(self, parameters):
        # Pass through SymbolicFunction
        symbolic_functions = [x for x in parameters if isinstance(x, SymbolicFunction)]
        dimension_dependency_mapper = dict()
        tensor_arguments = []
        for f in symbolic_functions:
            argument = ArgumentVisitor().visit(f)
            tensor_arguments.append(argument)
            for i, d in enumerate(f.indices):
                if d not in dimension_dependency_mapper:
                    dimension_dependency_mapper[d] = []
                dimension_dependency_mapper[d].append(Dependency("gets_value_from", argument, param=i))

        for arg in self.dle_arguments:
            d = arg.argument
            if d not in dimension_dependency_mapper:
                    dimension_dependency_mapper[d] = []
            dimension_dependency_mapper[d].append(Dependency("gets_value_from", derive_dle_argument_value, param=arg))
            
        # Record dependencies in Dimensions
        dimension_parameter_mapper = {}
        for dim, deps in dimension_dependency_mapper.items():
            dimension_parameter_mapper[dim] = DimensionParameter(dim, deps)

        # Dimensions that are in parameters but not directly referenced in the expressions
        for dim in [x for x in parameters if isinstance(x, Dimension) and x not in dimension_dependency_mapper.keys()]:
            dimension_parameter_mapper[dim] = DimensionParameter(dim, [])

        for dim in [x for x in parameters if isinstance(x, Dimension) and x.is_Stepping]:
           # dimension_parameter_mapper[dim].dependencies.append(Dependency(Dependency.GETS_VALUE_FROM, dimension_parameter_mapper[dim.parent]))
            dimension_parameter_mapper[dim.parent].dependencies.append(Dependency(Dependency.GETS_VALUE_FROM, dimension_parameter_mapper[dim]))

        dimension_parameters = list(dimension_parameter_mapper.values())
        
        # Pass Dimensions
        scalar_arguments = []
        for dimension_parameter in dimension_parameters:
            scalar_arguments += ArgumentVisitor().visit(dimension_parameter)

        other_arguments = [ArgumentVisitor().visit(x) for x in parameters if x not in tensor_arguments + scalar_arguments]
            
        return tensor_arguments + dimension_parameters + scalar_arguments + other_arguments

    def _extract_children_of_composites(self, kwargs):
        new_params = {}
        # If we've been passed CompositeFunction objects as kwargs,
        # they might have children that need to be substituted as well.
        for k, v in kwargs.items():
            if isinstance(v, CompositeFunction):
                orig_param_l = [i for i in self.parameters if i.name == k]
                # If I have been passed a parameter, I must have seen it before
                if len(orig_param_l) == 0:
                    raise InvalidArgument("Parameter %s does not exist in expressions " +
                                          "passed to this Operator" % k)
                # We've made sure the list isn't empty. Names should be unique so it
                # should have exactly one entry
                assert(len(orig_param_l) == 1)
                orig_param = orig_param_l[0]
                # Pull out the children and add them to kwargs
                for orig_child, new_child in zip(orig_param.children, v.children):
                    new_params[orig_child.name] = new_child
        kwargs.update(new_params)
        return kwargs

    def _dle_arguments(self, dim_sizes):
        # Add user-provided block sizes, if any
        dle_arguments = OrderedDict()
        autotune = True
        for i in self.dle_arguments:
            dim_size = dim_sizes.get(i.original_dim.name, None)
            if dim_size is None:
                error('Unable to derive size of dimension %s from defaults. '
                      'Please provide an explicit value.' % i.original_dim.name)
                raise InvalidArgument('Unknown dimension size')
            if i.value:
                try:
                    dle_arguments[i.argument.name] = i.value(dim_size)
                except TypeError:
                    dle_arguments[i.argument.name] = i.value
                    autotune = False
            else:
                dle_arguments[i.argument.name] = dim_size
        return dle_arguments, autotune

    def _derive_values(self, kwargs):
        # Use kwargs
        values = OrderedDict()
        dimension_values = OrderedDict()
        
        for i in self.arguments:
            values[i] = get_value(i, kwargs.pop(i.name, None), values)

        for i in self.dims:
            dimension_values[i] = kwargs.pop(i.name, None)

        # Make sure we've used all arguments passed
        if len(kwargs) > 0:
            raise InvalidArgument("Unknown arguments passed: " + ", ".join(kwargs.keys()))

        # Derive values for other arguments
        for i in self.arguments:
            if values[i] is None:
                known_values = OrderedDict(chain(values.items(), dimension_values.items()))
                provided_values = [get_value(i, x, known_values) for x in i.gets_value_from]
                assert(len(provided_values) > 0)
        
                if len(provided_values) == 1:
                    values[i] = provided_values[0]
                else:
                    values[i] = reduce(provided_values)
        
        # Second pass to evaluate any Unevaluated dependencies from the first pass
        for k, v in values.items():
            if isinstance(v, UnevaluatedDependency):
                values[k] = v.evaluate(values)
        return values

    def _verify(self, values):
        verify = True
        for i in values:
            verify = verify and all(verify(i, x) for x in i.verified_by)
        return verify

    @property
    def dimensions(self):
        return [x for x in self.parameters if isinstance(x, Dimension)]


class ArgumentVisitor(GenericVisitor):
    """ Visits types to return their runtime arguments
    """
    def visit_SymbolicFunction(self, o):
        return TensorArgument(o)
    
    def visit_DimensionParameter(self, o):
        dependency = Dependency("gets_value_from", o)
        size = ScalarArgument(o.provider.size_name, [dependency])
        start = ScalarArgument(o.provider.start_name, [dependency])
        end = ScalarArgument(o.provider.end_name, [dependency])
        return [size, start, end]
    
    def visit_Object(self, o):
        return PtrArgument(o)

    def visit_Array(self, o):
        return TensorArgument(o)

    def visit_Scalar(self, o):
        dependency = Dependency("gets_value_from", o)
        return ScalarArgument(o.name, o, dtype=o.dtype)

    def visit_Constant(self, o):
        # TODO: Add option for delayed query of default value
        dependency = Dependency("gets_value_from", o)
        return ScalarArgument(o.name, [dependency], dtype=o.dtype)

    
class ValueVisitor(GenericVisitor):
    """Visits types to derive their value
    """
    def __init__(self, consumer, known_values):
        self.consumer = consumer
        self.known_values = known_values
        super(ValueVisitor, self).__init__()
        
    def visit_Function(self, o, param=None):
        assert(isinstance(self.consumer, TensorArgument))
        return o.data

    def visit_Constant(self, o, param=None):
        return o.data

    def visit_Dependency(self, o):
        return self.visit(o.obj, o.param)

    def visit_function(self, o, param=None):
        return o(self.consumer, self.known_values, param)

    def visit_Object(self, o, param=None):
        return o.value

    def visit_object(self, o, param=None):
        return o

    def visit_DimensionParameter(self, o, param=None):
        # We are being asked to provide a default value for dim_start
        if self.consumer.name == o.provider.start_name:
            return 0
        provided_values = [get_value(o, x, self.known_values) for x in o.gets_value_from]
        if o in self.known_values and not isinstance(self.known_values[o], UnevaluatedDependency) and self.known_values[o] is not None:
            provided_values = [self.known_values[o]]
        if len(provided_values) > 1:
            if not all(x is not None for x in provided_values):
                unknown_args = [x.obj for x in o.gets_value_from if get_value(o, x, self.known_values) is None]
                def late_evaluate_dim_size(consumer, known_values, partial_values):
                    known = []
                    try:
                        new_values = [known_values[x] for x in unknown_args]
                        known = [x for x in new_values if x is not None]
                    except KeyError:
                        pass
                    return reduce(max, partial_values + known)
                return UnevaluatedDependency(o, late_evaluate_dim_size, [x for x in provided_values if x is not None])
            value = reduce(max, provided_values)
        elif len(provided_values) == 1:
            value = provided_values[0]
        else:
            value = None
        return value

    def visit_TensorArgument(self, o, param):
        assert(isinstance(self.consumer, DimensionParameter))
        return self.known_values[o].shape[param]


class Dependency(object):
    """ Object that represents an edge between two nodes on a dependency graph
        Dependencies are directional, i.e. A -> B != B -> A
        :param dependency_type: A dependency can either by of type "gets_value_from"
               in which case this dependency will be followed for value derivation
               or it can be of type "verified_by" in which case this dependency will be 
               followed for verification. Both types of dependencies may exist between a
               pair of nodes. However, only a single dependency of a type may exist
               between any pair of nodes.
        :param obj: Dependencies have a source node and a target node. The source node
                    will store the dependency object on itself. The obj referred to here
                    is the target node of the dependency. 
        :param param: An optional parameter that might be required to evaluate the
                      relationship being defined by this Dependency. e.g. when a Dimension
                      derives its value from a SymbolicFunction's shape, this param carries
                      the index of a dimension in the SymbolicFunction's shape. 
    """
    GETS_VALUE_FROM = "gets_value_from"
    VERIFIED_BY = "verified_by"
    _types = ["gets_value_from", "verified_by"]
    def __init__(self, dependency_type, obj, param=None):
        assert(dependency_type in self._types)
        self.dependency_type = dependency_type
        self.obj = obj
        self.param = param

    def __repr__(self):
        return "(" + self.dependency_type + ":" + str(self.obj) + ")"


class UnevaluatedDependency(object):
    def __init__(self, consumer, evaluator, extra_param=None):
        self.consumer = consumer
        self.evaluator = evaluator
        self.extra_param = extra_param

    def evaluate(self, known_values):
        return self.evaluator(self.consumer, known_values, self.extra_param)


def runtime_arguments(parameters):
    return flatten([ArgumentVisitor().visit(p) for p in parameters])


def log_args(arguments):
    arg_str = []
    for k, v in arguments.items():
        if hasattr(v, 'shape'):
            arg_str.append('(%s, shape=%s, L2 Norm=%d, type=%s)' %
                           (k, str(v.shape), np.linalg.norm(v.view()), type(v)))
        else:
            arg_str.append('(%s, value=%s, type=%s)' % (k, str(v), type(v)))
    print("Passing Arguments: " + ", ".join(arg_str))


def find_argument_by_name(name, haystack):
    filtered = [(k, v) for k, v in haystack.items() if k.name == name]
    assert(len(filtered) < 2)
    if len(filtered) == 1:
        return filtered[0][1]
    else:
        return None


def runtime_dim_extent(dimension, values):
    try:
        return find_argument_by_name(dimension.end_name, values) - find_argument_by_name(dimension.start_name, values)
    except (KeyError, TypeError):
        return None


def derive_dle_argument_value(blocked_dim, known_values, dle_argument):
    dim_size = runtime_dim_extent(dle_argument.original_dim, known_values)
    if dim_size is None:
        return UnevaluatedDependency(blocked_dim, derive_dle_argument_value, dle_argument)
    value = None
    if dle_argument.value:
        try:
            value = dle_argument.value(dim_size)
        except TypeError:
            value = dle_argument.value
    else:
        value = dim_size
    return value


def get_value(consumer, provider, known_values):
    return ValueVisitor(consumer, known_values).visit(provider)
