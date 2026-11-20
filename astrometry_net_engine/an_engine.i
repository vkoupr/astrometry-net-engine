// Copyright (c) 2010-2026 Vladimir Kouprianov (v.kouprianov@gmail.com)
// SPDX-License-Identifier: GPL-3.0-only
// Adapted 2026-11-15: instance-scoped callbacks and stable buffer interface.

%module(threads="1") an_engine
%feature("autodoc", "1");

%include <typemaps.i>
// Fixed-width match metadata must be Python integers, rather than opaque pointer proxies.
%include <stdint.i>

%{
#include <time.h>
#include <limits.h>
#include "astrometry/index.h"
#include "astrometry/starxy.h"
#include "astrometry/sip.h"
#include "astrometry/matchobj.h"
#include "astrometry/solver.h"
#include "astrometry/fit-wcs.h"
#include "astrometry/tweak2.h"
#include "astrometry/healpix.h"
#include "astrometry/log.h"
#include "astrometry/tic.h"
%}

// Callbacks are scoped to one run; the Python API serializes native engine operations.
%{
typedef struct {
    PyObject *callable;
    PyObject *type, *value, *traceback;
    int canceled;
} callback_context;

static time_t python_timer_callback(void *userdata) {
    callback_context *ctx = (callback_context *)userdata;
    PyGILState_STATE state = PyGILState_Ensure();
    PyObject *result = NULL;
    long delay = 0;
    if (!ctx->type && !ctx->canceled) {
        result = PyObject_CallObject(ctx->callable, NULL);
        if (result) {
            delay = PyLong_AsLong(result);
            Py_DECREF(result);
            if (!PyErr_Occurred() && (delay < 0 || delay > INT_MAX))
                PyErr_SetString(PyExc_ValueError, "Timer callback must return an integer in [0, INT_MAX]");
        }
        if (PyErr_Occurred()) {
            PyErr_Fetch(&ctx->type, &ctx->value, &ctx->traceback);
            delay = 0;
        }
        if (!delay) ctx->canceled = 1;
    }
    PyGILState_Release(state);
    return (time_t)delay;
}

static int solver_run_with_callback(solver_t *solver, PyObject *callback) {
    callback_context ctx = {callback, NULL, NULL, NULL, 0};
    time_t (*previous_timer)(void *) = solver->timer_callback;
    void *previous_userdata = solver->userdata;
    if (callback != Py_None && !PyCallable_Check(callback)) {
        PyErr_SetString(PyExc_TypeError, "timer_callback must be callable or None");
        return 0;
    }
    solver->timer_callback = callback == Py_None ? NULL : python_timer_callback;
    solver->userdata = &ctx;
    Py_BEGIN_ALLOW_THREADS
    solver_run(solver);
    /* Upstream updates this only on timer/match paths; include unsuccessful search time too. */
    {
        double usertime, systime;
        get_resource_stats(&usertime, &systime, NULL);
        solver->timeused = usertime + systime - solver->starttime;
        if (solver->timeused < 0.0) solver->timeused = 0.0;
    }
    Py_END_ALLOW_THREADS
    solver->timer_callback = previous_timer;
    solver->userdata = previous_userdata;
    if (ctx.type) PyErr_Restore(ctx.type, ctx.value, ctx.traceback);
    return !ctx.canceled;
}

/* SWIG copies the returned string; do not dereference a missing best index after an unsuccessful run. */
static const char *solver_best_index_name(const solver_t *solver) {
    return solver->have_best_match && solver->best_index ? solver->best_index->indexname : NULL;
}
%}

%feature("nothread") solver_run_with_callback;
%exception solver_run_with_callback {
    $action
    if (PyErr_Occurred()) SWIG_fail;
}
int solver_run_with_callback(solver_t *solver, PyObject *callback);
const char *solver_best_index_name(const solver_t *solver);

// Handle arrays (e.g. crpix)

%include "carrays.i"
%include "cpointer.i"
%array_class(double, DoubleArray);
%array_class(int, IntArray);
%array_class(unsigned int, UIntArray);
%pointer_cast(void *, double *, voidp_to_doublep);
%pointer_cast(void *, int *, voidp_to_intp);
%pointer_cast(void *, unsigned int *, voidp_to_uintp);


// Handle int* and double* output arguments

%typemap(argout) double * {
    PyObject *o, *o2, *o3;
    o = SWIG_From_double(*$1);
    if ((!$result) || ($result == Py_None)) {
        $result = o;
    } else {
        if (!PyTuple_Check($result)) {
            PyObject *o2 = $result;
            $result = PyTuple_New(1);
            PyTuple_SetItem($result,0,o2);
        }
        o3 = PyTuple_New(1);
        PyTuple_SetItem(o3, 0, o);
        o2 = $result;
        $result = PySequence_Concat(o2, o3);
        Py_DECREF(o2);
        Py_DECREF(o3);
    }
}

%typemap(in,numinputs=0) double * (double temp) {
    $1 = &temp;
}


%typemap(argout) int * {
    PyObject *o, *o2, *o3;
    o = SWIG_From_int(*$1);
    if ((!$result) || ($result == Py_None)) {
        $result = o;
    } else {
        if (!PyTuple_Check($result)) {
            PyObject *o2 = $result;
            $result = PyTuple_New(1);
            PyTuple_SetItem($result,0,o2);
        }
        o3 = PyTuple_New(1);
        PyTuple_SetItem(o3, 0, o);
        o2 = $result;
        $result = PySequence_Concat(o2, o3);
        Py_DECREF(o2);
        Py_DECREF(o3);
    }
}

%typemap(in,numinputs=0) int * (int temp) {
    $1 = &temp;
}

// Use NumPy arrays via Python's stable buffer protocol in starxy_set_*_array()

%typemap(in) const double * (Py_buffer view = {0}) {
    if (PyObject_GetBuffer($input, &view, PyBUF_FORMAT | PyBUF_C_CONTIGUOUS) < 0)
        SWIG_fail;
    if (view.ndim != 1 || view.itemsize != sizeof(double) || !view.format || strcmp(view.format, "d") != 0) {
        PyErr_SetString(PyExc_ValueError, "Expected a contiguous one-dimensional native float64 buffer");
        SWIG_fail;
    }
    $1 = (const double *)view.buf;
}
%typemap(argout) const double * {}
%typemap(freearg) const double * {
    if (view$argnum.obj) PyBuffer_Release(&view$argnum);
}


// starxy_get() returns an xy pair

%typemap(argout) double *xy {
    PyObject *o, *o2, *o3;
    o = PyTuple_New(2);
    PyTuple_SetItem(o, 0, SWIG_From_double($1[0]));
    PyTuple_SetItem(o, 1, SWIG_From_double($1[1]));
    if ((!$result) || ($result == Py_None)) {
        $result = o;
    } else {
        if (!PyTuple_Check($result)) {
            PyObject *o2 = $result;
            $result = PyTuple_New(1);
            PyTuple_SetItem($result,0,o2);
        }
        o3 = PyTuple_New(1);
        PyTuple_SetItem(o3, 0, o);
        o2 = $result;
        $result = PySequence_Concat(o2, o3);
        Py_DECREF(o2);
        Py_DECREF(o3);
    }
}

%typemap(in,numinputs=0) double *xy (double temp[2]) {
    $1 = temp;
}


// In/out tan_t and sip_t

%typemap(in) sip_t *sip (void *argp = 0, int res = 0) {
    res = SWIG_ConvertPtr($input, &argp, $descriptor, $disown | %convertptr_flags);
    if (!SWIG_IsOK(res)) {
	    %argument_fail(res, "$type", $symname, $argnum);
    }
    $1 = %reinterpret_cast(argp, $ltype);
}

%typemap(argout) sip_t *sip {
    $result = SWIG_NewPointerObj(SWIG_as_voidptr($1), SWIGTYPE_p_sip_t, 0);
}


%typemap(in) tan_t *wcstan (void *argp = 0, int res = 0) {
    res = SWIG_ConvertPtr($input, &argp, $descriptor, $disown | %convertptr_flags);
    if (!SWIG_IsOK(res)) {
	    %argument_fail(res, "$type", $symname, $argnum);
    }
    $1 = %reinterpret_cast(argp, $ltype);
}

%typemap(argout) tan_t *wcstan {
    $result = SWIG_NewPointerObj(SWIG_as_voidptr($1), SWIGTYPE_p_tan_t, 0);
}


// Wrap the basic engine functionality

%ignore matchobj_compute_overlap; // unimplemented

%define WarnUnusedResult %enddef
%include "astrometry/an-bool.h"
%include "astrometry/index.h"
%include "astrometry/qfits_table.h"
%include "astrometry/fitstable.h"
%include "astrometry/starxy.h"
%include "astrometry/starkd.h"
%include "astrometry/sip.h"
%include "astrometry/fit-wcs.h"
%include "astrometry/matchobj.h"
%include "astrometry/solver.h"
%include "astrometry/tweak2.h"


// Misc. extra functions to wrap

// The high-level index filter needs only the distance, not the optional closest point.
%typemap(in,numinputs=0) double *closestradec { $1 = NULL; }
%typemap(argout) double *closestradec {}

double healpix_distance_to_radec(int hp, int Nside, double ra, double dec, double* closestradec);
int healpix_within_range_of_radec(int hp, int Nside, double ra, double dec, double radius);

void log_init(int);
