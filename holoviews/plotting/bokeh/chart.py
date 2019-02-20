from __future__ import absolute_import, division, unicode_literals

from collections import defaultdict

import numpy as np
import param
from bokeh.models import CategoricalColorMapper, CustomJS, Whisker, Range1d
from bokeh.models.tools import BoxSelectTool
from bokeh.transform import jitter

from ...core.data import Dataset
from ...core.dimension import dimension_name
from ...core.util import OrderedDict, max_range, basestring, dimension_sanitizer, isfinite, range_pad
from ...element import Bars
from ...operation import interpolate_curve
from ...util.transform import dim
from ..util import compute_sizes, get_min_distance, get_axis_padding
from .element import ElementPlot, ColorbarPlot, LegendPlot
from .styles import (expand_batched_style, line_properties, fill_properties,
                     mpl_to_bokeh, rgb2hex)
from .util import categorize_array


class PointPlot(LegendPlot, ColorbarPlot):

    jitter = param.Number(default=None, bounds=(0, None), doc="""
      The amount of jitter to apply to offset the points along the x-axis.""")

    # Deprecated parameters

    color_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
        Deprecated in favor of color style mapping, e.g. `color=dim('color')`""")

    size_index = param.ClassSelector(default=None, class_=(basestring, int),
                                     allow_None=True, doc="""
        Deprecated in favor of size style mapping, e.g. `size=dim('size')`""")

    scaling_method = param.ObjectSelector(default="area",
                                          objects=["width", "area"],
                                          doc="""
        Deprecated in favor of size style mapping, e.g.
        size=dim('size')**2.""")

    scaling_factor = param.Number(default=1, bounds=(0, None), doc="""
      Scaling factor which is applied to either the width or area
      of each point, depending on the value of `scaling_method`.""")

    size_fn = param.Callable(default=np.abs, doc="""
      Function applied to size values before applying scaling,
      to remove values lower than zero.""")

    style_opts = (['cmap', 'palette', 'marker', 'size', 'angle'] +
                  line_properties + fill_properties)

    _plot_methods = dict(single='scatter', batched='scatter')
    _batched_style_opts = line_properties + fill_properties + ['size']

    def _get_size_data(self, element, ranges, style):
        data, mapping = {}, {}
        sdim = element.get_dimension(self.size_index)
        ms = style.get('size', np.sqrt(6))
        if sdim and ((isinstance(ms, basestring) and ms in element) or isinstance(ms, dim)):
            self.param.warning(
                "Cannot declare style mapping for 'size' option and "
                "declare a size_index; ignoring the size_index.")
            sdim = None
        if not sdim or self.static_source:
            return data, mapping

        map_key = 'size_' + sdim.name
        ms = ms**2
        sizes = element.dimension_values(self.size_index)
        sizes = compute_sizes(sizes, self.size_fn,
                              self.scaling_factor,
                              self.scaling_method, ms)
        if sizes is None:
            eltype = type(element).__name__
            self.param.warning(
                '%s dimension is not numeric, cannot use to scale %s size.'
                % (sdim.pprint_label, eltype))
        else:
            data[map_key] = np.sqrt(sizes)
            mapping['size'] = map_key
        return data, mapping


    def get_data(self, element, ranges, style):
        dims = element.dimensions(label=True)

        xidx, yidx = (1, 0) if self.invert_axes else (0, 1)
        mapping = dict(x=dims[xidx], y=dims[yidx])
        data = {}

        if not self.static_source or self.batched:
            xdim, ydim = dims[xidx], dims[yidx]
            data[xdim] = element.dimension_values(xidx)
            data[ydim] = element.dimension_values(yidx)
            self._categorize_data(data, (xdim, ydim), element.dimensions())

        cdata, cmapping = self._get_color_data(element, ranges, style)
        data.update(cdata)
        mapping.update(cmapping)

        sdata, smapping = self._get_size_data(element, ranges, style)
        data.update(sdata)
        mapping.update(smapping)

        if 'angle' in style and isinstance(style['angle'], (int, float)):
            style['angle'] = np.deg2rad(style['angle'])

        if self.jitter:
            if self.invert_axes:
                mapping['y'] = jitter(dims[yidx], self.jitter,
                                      range=self.handles['y_range'])
            else:
                mapping['x'] = jitter(dims[xidx], self.jitter,
                                      range=self.handles['x_range'])

        self._get_hover_data(data, element)
        return data, mapping, style


    def get_batched_data(self, element, ranges):
        data = defaultdict(list)
        zorders = self._updated_zorders(element)
        for (key, el), zorder in zip(element.data.items(), zorders):
            self.param.set_param(**self.lookup_options(el, 'plot').options)
            style = self.lookup_options(element.last, 'style')
            style = style.max_cycles(len(self.ordering))[zorder]
            eldata, elmapping, style = self.get_data(el, ranges, style)
            for k, eld in eldata.items():
                data[k].append(eld)

            # Skip if data is empty
            if not eldata:
                continue

            # Apply static styles
            nvals = len(list(eldata.values())[0])
            sdata, smapping = expand_batched_style(style, self._batched_style_opts,
                                                   elmapping, nvals)
            elmapping.update(smapping)
            for k, v in sdata.items():
                data[k].append(v)

            if 'hover' in self.handles:
                for d, k in zip(element.dimensions(), key):
                    sanitized = dimension_sanitizer(d.name)
                    data[sanitized].append([k]*nvals)

        data = {k: np.concatenate(v) for k, v in data.items()}
        return data, elmapping, style

class StickPlot(ColorbarPlot):

    rescale_lengths = param.Boolean(default=True, doc="""
        Whether the lengths will be rescaled to take into account the
        smallest non-zero distance between two vectors.""")

    # Deprecated parameters

    color_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
        Deprecated in favor of dimension value transform on color option,
        e.g. `color=dim('Magnitude')`.
        """)

    size_index = param.ClassSelector(default=None, class_=(basestring, int),
                                     allow_None=True, doc="""
        Deprecated in favor of the magnitude option, e.g.
        `magnitude=dim('Magnitude')`.
        """)

    style_opts = line_properties + ['scale', 'cmap']

    _nonvectorized_styles = ['scale', 'cmap']

    _plot_methods = dict(single='ray')

    def _glyph_properties(self, *args):
        properties = super(StickPlot, self)._glyph_properties(*args)
        properties.pop('scale', None)
        return properties

    def _get_lengths(self, element, style, x_is_datetime):
        input_scale = style.pop('scale', 1)
        if x_is_datetime:
            # when x is datetime and y is float, then holoviews scales distance
            # in nanoseconds, but bokeh works in milliseconds...
            # needs a more fundamental fix than this
            input_scale *= 1e6

        magnitudes = element.dimension_values(3).copy()
        if self.rescale_lengths:
            base_dist = get_min_distance(element)
            magnitudes = magnitudes * base_dist
        return magnitudes/input_scale

    def get_data(self, element, ranges, style):
        # Get x, y, angle, magnitude and color data
        rads = element.dimension_values(2)
        if self.invert_axes:
            xidx, yidx = (1, 0)
            rads = np.pi/2 - rads
        else:
            xidx, yidx = (0, 1)

        # Compute ray positions
        xs = element.dimension_values(xidx)
        ys = element.dimension_values(yidx)

        # is abscissa datetime axis?
        # (for length of Ray, bokeh considers abscissa only)
        x_is_datetime = np.issubdtype(xs.dtype, np.datetime64)
        lens = self._get_lengths(element, style, x_is_datetime)

        cdim = element.get_dimension(self.color_index)
        cdata, cmapping = self._get_color_data(element, ranges, style,
                                               name='line_color')

        color = None
        if cdim:
            color = cdata.get(cdim.name)

        data = {'x': xs, 'y': ys, 'length': lens, 'angle': rads}
        mapping = dict(x='x', y='y', length='length', angle='angle')
        if cdim and color is not None:
            data[cdim.name] = color
            mapping.update(cmapping)

        return (data, mapping, style)


class VectorFieldPlot(ColorbarPlot):

    arrow_heads = param.Boolean(default=True, doc="""
        Whether or not to draw arrow heads.""")

    magnitude = param.ClassSelector(class_=(basestring, dim), doc="""
        Dimension or dimension value transform that declares the magnitude
        of each vector. Magnitude is expected to be scaled between 0-1,
        by default the magnitudes are rescaled relative to the minimum
        distance between vectors, this can be disabled with the
        rescale_lengths option.""")

    pivot = param.ObjectSelector(default='mid', objects=['mid', 'tip', 'tail'],
                                 doc="""
        The point around which the arrows should pivot valid options
        include 'mid', 'tip' and 'tail'.""")

    rescale_lengths = param.Boolean(default=True, doc="""
        Whether the lengths will be rescaled to take into account the
        smallest non-zero distance between two vectors.""")

    # Deprecated parameters

    color_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
        Deprecated in favor of dimension value transform on color option,
        e.g. `color=dim('Magnitude')`.
        """)

    size_index = param.ClassSelector(default=None, class_=(basestring, int),
                                     allow_None=True, doc="""
        Deprecated in favor of the magnitude option, e.g.
        `magnitude=dim('Magnitude')`.
        """)

    normalize_lengths = param.Boolean(default=True, doc="""
        Deprecated in favor of rescaling length using dimension value
        transforms using the magnitude option, e.g.
        `dim('Magnitude').norm()`.""")

    style_opts = line_properties + ['scale', 'cmap']

    _nonvectorized_styles = ['scale', 'cmap']

    _plot_methods = dict(single='segment')

    def _get_lengths(self, element, ranges):
        size_dim = element.get_dimension(self.size_index)
        mag_dim = self.magnitude
        if size_dim and mag_dim:
            self.param.warning(
                "Cannot declare style mapping for 'magnitude' option "
                "and declare a size_index; ignoring the size_index.")
        elif size_dim:
            mag_dim = size_dim
        elif isinstance(mag_dim, basestring):
            mag_dim = element.get_dimension(mag_dim)

        (x0, x1), (y0, y1) = (element.range(i) for i in range(2))
        if mag_dim:
            if isinstance(mag_dim, dim):
                magnitudes = mag_dim.apply(element, flat=True)
            else:
                magnitudes = element.dimension_values(mag_dim)
                _, max_magnitude = ranges[dimension_name(mag_dim)]['combined']
                if self.normalize_lengths and max_magnitude != 0:
                    magnitudes = magnitudes / max_magnitude
            if self.rescale_lengths:
                base_dist = get_min_distance(element)
                magnitudes *= base_dist
        else:
            magnitudes = np.ones(len(element))
            if self.rescale_lengths:
                base_dist = get_min_distance(element)
                magnitudes *= base_dist

        return magnitudes

    def _glyph_properties(self, *args):
        properties = super(VectorFieldPlot, self)._glyph_properties(*args)
        properties.pop('scale', None)
        return properties


    def get_data(self, element, ranges, style):
        input_scale = style.pop('scale', 1.0)

        # Get x, y, angle, magnitude and color data
        rads = element.dimension_values(2)
        if self.invert_axes:
            xidx, yidx = (1, 0)
            rads = rads+1.5*np.pi
        else:
            xidx, yidx = (0, 1)
        lens = self._get_lengths(element, ranges)/input_scale
        cdim = element.get_dimension(self.color_index)
        cdata, cmapping = self._get_color_data(element, ranges, style,
                                               name='line_color')

        # Compute segments and arrowheads
        xs = element.dimension_values(xidx)
        ys = element.dimension_values(yidx)

        # Compute offset depending on pivot option
        xoffsets = np.cos(rads)*lens/2.
        yoffsets = np.sin(rads)*lens/2.
        if self.pivot == 'mid':
            nxoff, pxoff = xoffsets, xoffsets
            nyoff, pyoff = yoffsets, yoffsets
        elif self.pivot == 'tip':
            nxoff, pxoff = 0, xoffsets*2
            nyoff, pyoff = 0, yoffsets*2
        elif self.pivot == 'tail':
            nxoff, pxoff = xoffsets*2, 0
            nyoff, pyoff = yoffsets*2, 0
        x0s, x1s = (xs + nxoff, xs - pxoff)
        y0s, y1s = (ys + nyoff, ys - pyoff)

        color = None
        if self.arrow_heads:
            arrow_len = (lens/4.)
            xa1s = x0s - np.cos(rads+np.pi/4)*arrow_len
            ya1s = y0s - np.sin(rads+np.pi/4)*arrow_len
            xa2s = x0s - np.cos(rads-np.pi/4)*arrow_len
            ya2s = y0s - np.sin(rads-np.pi/4)*arrow_len
            x0s = np.tile(x0s, 3)
            x1s = np.concatenate([x1s, xa1s, xa2s])
            y0s = np.tile(y0s, 3)
            y1s = np.concatenate([y1s, ya1s, ya2s])
            if cdim and cdim.name in cdata:
                color = np.tile(cdata[cdim.name], 3)
        elif cdim:
            color = cdata.get(cdim.name)

        data = {'x0': x0s, 'x1': x1s, 'y0': y0s, 'y1': y1s}
        mapping = dict(x0='x0', x1='x1', y0='y0', y1='y1')
        if cdim and color is not None:
            data[cdim.name] = color
            mapping.update(cmapping)

        return (data, mapping, style)



class CurvePlot(ElementPlot):

    interpolation = param.ObjectSelector(objects=['linear', 'steps-mid',
                                                  'steps-pre', 'steps-post'],
                                         default='linear', doc="""
        Defines how the samples of the Curve are interpolated,
        default is 'linear', other options include 'steps-mid',
        'steps-pre' and 'steps-post'.""")

    style_opts = line_properties
    _nonvectorized_styles = line_properties

    _plot_methods = dict(single='line', batched='multi_line')
    _batched_style_opts = line_properties

    def get_data(self, element, ranges, style):
        xidx, yidx = (1, 0) if self.invert_axes else (0, 1)
        x = element.get_dimension(xidx).name
        y = element.get_dimension(yidx).name
        if self.static_source and not self.batched:
            return {}, dict(x=x, y=y), style

        if 'steps' in self.interpolation:
            element = interpolate_curve(element, interpolation=self.interpolation)
        data = {x: element.dimension_values(xidx),
                y: element.dimension_values(yidx)}
        self._get_hover_data(data, element)
        self._categorize_data(data, (x, y), element.dimensions())
        return (data, dict(x=x, y=y), style)

    def _hover_opts(self, element):
        if self.batched:
            dims = list(self.hmap.last.kdims)
            line_policy = 'prev'
        else:
            dims = list(self.overlay_dims.keys())+element.dimensions()
            line_policy = 'nearest'
        return dims, dict(line_policy=line_policy)

    def get_batched_data(self, overlay, ranges):
        data = defaultdict(list)

        zorders = self._updated_zorders(overlay)
        for (key, el), zorder in zip(overlay.data.items(), zorders):
            self.param.set_param(**self.lookup_options(el, 'plot').options)
            style = self.lookup_options(el, 'style')
            style = style.max_cycles(len(self.ordering))[zorder]
            eldata, elmapping, style = self.get_data(el, ranges, style)

            # Skip if data empty
            if not eldata:
                continue

            for k, eld in eldata.items():
                data[k].append(eld)

            # Apply static styles
            sdata, smapping = expand_batched_style(style, self._batched_style_opts,
                                                   elmapping, nvals=1)
            elmapping.update(smapping)
            for k, v in sdata.items():
                data[k].append(v[0])

            for d, k in zip(overlay.kdims, key):
                sanitized = dimension_sanitizer(d.name)
                data[sanitized].append(k)
        data = {opt: vals for opt, vals in data.items()
                if not any(v is None for v in vals)}
        mapping = {{'x': 'xs', 'y': 'ys'}.get(k, k): v
                   for k, v in elmapping.items()}
        return data, mapping, style



class HistogramPlot(ColorbarPlot):

    style_opts = line_properties + fill_properties + ['cmap']
    _plot_methods = dict(single='quad')

    _nonvectorized_styles = ['line_dash']

    def get_data(self, element, ranges, style):
        if self.invert_axes:
            mapping = dict(top='right', bottom='left', left=0, right='top')
        else:
            mapping = dict(top='top', bottom=0, left='left', right='right')
        if self.static_source:
            data = dict(top=[], left=[], right=[])
        else:
            x = element.kdims[0]
            values = element.dimension_values(1)
            edges = element.interface.coords(element, x, edges=True)
            data = dict(top=values, left=edges[:-1], right=edges[1:])
            self._get_hover_data(data, element)
        return (data, mapping, style)

    def get_extents(self, element, ranges, range_type='combined'):
        ydim = element.get_dimension(1)
        s0, s1 = ranges[ydim.name]['soft']
        s0 = min(s0, 0) if isfinite(s0) else 0
        s1 = max(s1, 0) if isfinite(s1) else 0
        ranges[ydim.name]['soft'] = (s0, s1)
        return super(HistogramPlot, self).get_extents(element, ranges, range_type)



class SideHistogramPlot(HistogramPlot):

    style_opts = HistogramPlot.style_opts + ['cmap']

    height = param.Integer(default=125, doc="The height of the plot")

    width = param.Integer(default=125, doc="The width of the plot")

    show_title = param.Boolean(default=False, doc="""
        Whether to display the plot title.""")

    default_tools = param.List(default=['save', 'pan', 'wheel_zoom',
                                        'box_zoom', 'reset'],
        doc="A list of plugin tools to use on the plot.")

    _callback = """
    color_mapper.low = cb_data['geometry']['{axis}0'];
    color_mapper.high = cb_data['geometry']['{axis}1'];
    source.change.emit()
    main_source.change.emit()
    """

    def __init__(self, *args, **kwargs):
        super(SideHistogramPlot, self).__init__(*args, **kwargs)
        if self.invert_axes:
            self.default_tools.append('ybox_select')
        else:
            self.default_tools.append('xbox_select')


    def get_data(self, element, ranges, style):
        data, mapping, style = HistogramPlot.get_data(self, element, ranges, style)
        color_dims = [d for d in self.adjoined.traverse(lambda x: x.handles.get('color_dim'))
                      if d is not None]
        dim = color_dims[0] if color_dims else None
        cmapper = self._get_colormapper(dim, element, {}, {})
        if cmapper and dim in element.dimensions():
            data[dim.name] = [] if self.static_source else element.dimension_values(dim)
            mapping['fill_color'] = {'field': dim.name,
                                     'transform': cmapper}
        return (data, mapping, style)


    def _init_glyph(self, plot, mapping, properties):
        """
        Returns a Bokeh glyph object.
        """
        ret = super(SideHistogramPlot, self)._init_glyph(plot, mapping, properties)
        if not 'field' in mapping.get('fill_color', {}):
            return ret
        dim = mapping['fill_color']['field']
        sources = self.adjoined.traverse(lambda x: (x.handles.get('color_dim'),
                                                     x.handles.get('source')))
        sources = [src for cdim, src in sources if cdim == dim]
        tools = [t for t in self.handles['plot'].tools
                 if isinstance(t, BoxSelectTool)]
        if not tools or not sources:
            return
        box_select, main_source = tools[0], sources[0]
        handles = {'color_mapper': self.handles['color_mapper'],
                   'source': self.handles['source'],
                   'cds': self.handles['source'],
                   'main_source': main_source}
        axis = 'y' if self.invert_axes else 'x'
        callback = self._callback.format(axis=axis)
        if box_select.callback:
            box_select.callback.code += callback
            box_select.callback.args.update(handles)
        else:
            box_select.callback = CustomJS(args=handles, code=callback)
        return ret



class ErrorPlot(ColorbarPlot):

    style_opts = line_properties + ['lower_head', 'upper_head']

    _nonvectorized_styles = ['line_dash']

    _mapping = dict(base="base", upper="upper", lower="lower")

    _plot_methods = dict(single=Whisker)

    def get_data(self, element, ranges, style):
        mapping = dict(self._mapping)
        if self.static_source:
            return {}, mapping, style

        base = element.dimension_values(0)
        ys = element.dimension_values(1)
        if len(element.vdims) > 2:
            neg, pos = (element.dimension_values(vd) for vd in element.vdims[1:3])
            lower, upper = ys-neg, ys+pos
        else:
            err = element.dimension_values(2)
            lower, upper = ys-err, ys+err
        data = dict(base=base, lower=lower, upper=upper)

        if self.invert_axes:
            mapping['dimension'] = 'width'
        else:
            mapping['dimension'] = 'height'
        self._categorize_data(data, ('base',), element.dimensions())
        return (data, mapping, style)


    def _init_glyph(self, plot, mapping, properties):
        """
        Returns a Bokeh glyph object.
        """
        properties.pop('legend', None)
        for prop in ['color', 'alpha']:
            if prop not in properties:
                continue
            pval = properties.pop(prop)
            line_prop = 'line_%s' % prop
            fill_prop = 'fill_%s' % prop
            if line_prop not in properties:
                properties[line_prop] = pval
            if fill_prop not in properties and fill_prop in self.style_opts:
                properties[fill_prop] = pval
        properties = mpl_to_bokeh(properties)
        plot_method = self._plot_methods['single']
        glyph = plot_method(**dict(properties, **mapping))
        plot.add_layout(glyph)
        return None, glyph



class SpreadPlot(ElementPlot):

    style_opts = line_properties + fill_properties
    _no_op_style = style_opts

    _plot_methods = dict(single='patch')

    _stream_data = False # Plot does not support streaming data

    def _split_area(self, xs, lower, upper):
        """
        Splits area plots at nans and returns x- and y-coordinates for
        each area separated by nans.
        """
        xnan = np.array([np.datetime64('nat') if xs.dtype.kind == 'M' else np.nan])
        ynan = np.array([np.datetime64('nat') if lower.dtype.kind == 'M' else np.nan])
        split = np.where(~isfinite(xs) | ~isfinite(lower) | ~isfinite(upper))[0]
        xvals = np.split(xs, split)
        lower = np.split(lower, split)
        upper = np.split(upper, split)
        band_x, band_y = [], []
        for i, (x, l, u) in enumerate(zip(xvals, lower, upper)):
            if i:
                x, l, u = x[1:], l[1:], u[1:]
            if not len(x):
                continue
            band_x += [np.append(x, x[::-1]), xnan]
            band_y += [np.append(l, u[::-1]), ynan]
        if len(band_x):
            xs = np.concatenate(band_x[:-1])
            ys = np.concatenate(band_y[:-1])
            return xs, ys
        return [], []

    def get_data(self, element, ranges, style):
        mapping = dict(x='x', y='y')
        xvals = element.dimension_values(0)
        mean = element.dimension_values(1)
        neg_error = element.dimension_values(2)
        pos_idx = 3 if len(element.dimensions()) > 3 else 2
        pos_error = element.dimension_values(pos_idx)
        lower = mean - neg_error
        upper = mean + pos_error

        band_x, band_y = self._split_area(xvals, lower, upper)
        if self.invert_axes:
            data = dict(x=band_y, y=band_x)
        else:
            data = dict(x=band_x, y=band_y)
        return data, mapping, style



class AreaPlot(SpreadPlot):

    _stream_data = False # Plot does not support streaming data

    def get_extents(self, element, ranges, range_type='combined'):
        vdims = element.vdims[:2]
        vdim = vdims[0].name
        if len(vdims) > 1:
            new_range = {}
            for r in ranges[vdim]:
                new_range[r] = max_range([ranges[vd.name][r] for vd in vdims])
            ranges[vdim] = new_range
        else:
            s0, s1 = ranges[vdim]['soft']
            s0 = min(s0, 0) if isfinite(s0) else 0
            s1 = max(s1, 0) if isfinite(s1) else 0
            ranges[vdim]['soft'] = (s0, s1)
        return super(AreaPlot, self).get_extents(element, ranges, range_type)

    def get_data(self, element, ranges, style):
        mapping = dict(x='x', y='y')
        xs = element.dimension_values(0)

        if len(element.vdims) > 1:
            bottom = element.dimension_values(2)
        else:
            bottom = np.zeros(len(element))
        top = element.dimension_values(1)

        band_xs, band_ys = self._split_area(xs, bottom, top)
        if self.invert_axes:
            data = dict(x=band_ys, y=band_xs)
        else:
            data = dict(x=band_xs, y=band_ys)
        return data, mapping, style



class SpikesPlot(ColorbarPlot):

    spike_length = param.Number(default=0.5, doc="""
      The length of each spike if Spikes object is one dimensional.""")

    position = param.Number(default=0., doc="""
      The position of the lower end of each spike.""")

    show_legend = param.Boolean(default=True, doc="""
        Whether to show legend for the plot.""")

    # Deprecated parameters

    color_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
        Deprecated in favor of color style mapping, e.g. `color=dim('color')`""")

    style_opts = (['color', 'cmap', 'palette'] + line_properties)

    _plot_methods = dict(single='segment')

    def get_extents(self, element, ranges, range_type='combined'):
        if len(element.dimensions()) > 1:
            ydim = element.get_dimension(1)
            s0, s1 = ranges[ydim.name]['soft']
            s0 = min(s0, 0) if isfinite(s0) else 0
            s1 = max(s1, 0) if isfinite(s1) else 0
            ranges[ydim.name]['soft'] = (s0, s1)
        l, b, r, t = super(SpikesPlot, self).get_extents(element, ranges, range_type)
        if len(element.dimensions()) == 1 and range_type != 'hard':
            if self.batched:
                bs, ts = [], []
                # Iterate over current NdOverlay and compute extents
                # from position and length plot options
                frame = self.current_frame or self.hmap.last
                for el in frame.values():
                    opts = self.lookup_options(el, 'plot').options
                    pos = opts.get('position', self.position)
                    length = opts.get('spike_length', self.spike_length)
                    bs.append(pos)
                    ts.append(pos+length)
                b, t = (np.nanmin(bs), np.nanmax(ts))
            else:
                b, t = self.position, self.position+self.spike_length
        return l, b, r, t

    def get_data(self, element, ranges, style):
        dims = element.dimensions()

        data = {}
        pos = self.position
        if len(element) == 0 or self.static_source:
            data = {'x': [], 'y0': [], 'y1': []}
        else:
            data['x'] = element.dimension_values(0)
            data['y0'] = np.full(len(element), pos)
            if len(dims) > 1:
                data['y1'] = element.dimension_values(1)+pos
            else:
                data['y1'] = data['y0']+self.spike_length

        if self.invert_axes:
            mapping = {'x0': 'y0', 'x1': 'y1', 'y0': 'x', 'y1': 'x'}
        else:
            mapping = {'x0': 'x', 'x1': 'x', 'y0': 'y0', 'y1': 'y1'}

        cdata, cmapping = self._get_color_data(element, ranges, dict(style))
        data.update(cdata)
        mapping.update(cmapping)
        self._get_hover_data(data, element)

        return data, mapping, style


class SideSpikesPlot(SpikesPlot):
    """
    SpikesPlot with useful defaults for plotting adjoined rug plot.
    """

    xaxis = param.ObjectSelector(default='top-bare',
                                 objects=['top', 'bottom', 'bare', 'top-bare',
                                          'bottom-bare', None], doc="""
        Whether and where to display the xaxis, bare options allow suppressing
        all axis labels including ticks and xlabel. Valid options are 'top',
        'bottom', 'bare', 'top-bare' and 'bottom-bare'.""")

    yaxis = param.ObjectSelector(default='right-bare',
                                      objects=['left', 'right', 'bare', 'left-bare',
                                               'right-bare', None], doc="""
        Whether and where to display the yaxis, bare options allow suppressing
        all axis labels including ticks and ylabel. Valid options are 'left',
        'right', 'bare' 'left-bare' and 'right-bare'.""")

    border = param.Integer(default=5, doc="Default borders on plot")

    height = param.Integer(default=50, doc="Height of plot")

    width = param.Integer(default=50, doc="Width of plot")



class BarPlot(ColorbarPlot, LegendPlot):
    """
    BarPlot allows generating single- or multi-category
    bar Charts, by selecting which key dimensions are
    mapped onto separate groups, categories and stacks.
    """

    stacked = param.Boolean(default=False, doc="""
       Whether the bars should be stacked or grouped.""")

    # Deprecated parameters

    color_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
        Deprecated in favor of color style mapping, e.g. `color=dim('color')`""")

    group_index = param.ClassSelector(default=1, class_=(basestring, int),
                                      allow_None=True, doc="""
       Deprecated; use stacked option instead.""")

    stack_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
       Deprecated; use stacked option instead.""")

    style_opts = line_properties + fill_properties + ['width', 'bar_width', 'cmap']

    _nonvectorized_styles = ['bar_width', 'cmap', 'width']

    _plot_methods = dict(single=('vbar', 'hbar'))

    # Declare that y-range should auto-range if not bounded
    _y_range_type = Range1d

    def get_extents(self, element, ranges, range_type='combined'):
        """
        Make adjustments to plot extents by computing
        stacked bar heights, adjusting the bar baseline
        and forcing the x-axis to be categorical.
        """
        if self.batched:
            overlay = self.current_frame
            element = Bars(overlay.table(), kdims=element.kdims+overlay.kdims,
                           vdims=element.vdims)
            for kd in overlay.kdims:
                ranges[kd.name]['combined'] = overlay.range(kd)

        extents = super(BarPlot, self).get_extents(element, ranges, range_type)
        xdim = element.kdims[0]
        ydim = element.vdims[0]

        # Compute stack heights
        if self.stacked or self.stack_index:
            ds = Dataset(element)
            pos_range = ds.select(**{ydim.name: (0, None)}).aggregate(xdim, function=np.sum).range(ydim)
            neg_range = ds.select(**{ydim.name: (None, 0)}).aggregate(xdim, function=np.sum).range(ydim)
            y0, y1 = max_range([pos_range, neg_range])
        else:
            y0, y1 = ranges[ydim.name]['combined']

        padding = 0 if self.overlaid else self.padding
        _, ypad, _ = get_axis_padding(padding)
        y0, y1 = range_pad(y0, y1, ypad, self.logy)

        # Set y-baseline
        if y0 < 0:
            y1 = max([y1, 0])
        elif self.logy:
            y0 = (ydim.range[0] or (10**(np.log10(y1)-2)) if y1 else 0.01)
        else:
            y0 = 0

        # Ensure x-axis is picked up as categorical
        x0 = xdim.pprint_value(extents[0])
        x1 = xdim.pprint_value(extents[2])
        return (x0, y0, x1, y1)


    def _get_factors(self, element):
        """
        Get factors for categorical axes.
        """
        gdim = None
        sdim = None
        if element.ndims == 1:
            pass
        elif not (self.stacked or self.stack_index):
            gdim = element.get_dimension(1)
        else:
            sdim = element.get_dimension(1)

        xdim, ydim = element.dimensions()[:2]
        xvals = element.dimension_values(0, False)
        xvals = [x if xvals.dtype.kind in 'SU' else xdim.pprint_value(x)
                 for x in xvals]
        if gdim and not sdim:
            gvals = element.dimension_values(gdim, False)
            xvals = sorted([(x, g) for x in xvals for g in gvals])
            is_str = gvals.dtype.kind in 'SU'
            xvals = [(x, g if is_str else gdim.pprint_value(g)) for (x, g) in xvals]
        coords = xvals, []
        if self.invert_axes: coords = coords[::-1]
        return coords


    def _get_axis_dims(self, element):
        if element.ndims > 1 and not (self.stacked or self.stack_index):
            xdims = element.kdims
        else:
            xdims = element.kdims[0]
        return (xdims, element.vdims[0])


    def get_stack(self, xvals, yvals, baselines, sign='positive'):
        """
        Iterates over a x- and y-values in a stack layer
        and appropriately offsets the layer on top of the
        previous layer.
        """
        bottoms, tops = [], []
        for x, y in zip(xvals, yvals):
            baseline = baselines[x][sign]
            if sign == 'positive':
                bottom = baseline
                top = bottom+y
                baseline = top
            else:
                top = baseline
                bottom = top+y
                baseline = bottom
            baselines[x][sign] = baseline
            bottoms.append(bottom)
            tops.append(top)
        return bottoms, tops


    def _glyph_properties(self, *args, **kwargs):
        props = super(BarPlot, self)._glyph_properties(*args, **kwargs)
        return {k: v for k, v in props.items() if k not in ['width', 'bar_width']}


    def _add_color_data(self, ds, ranges, style, cdim, data, mapping, factors, colors):
        cdata, cmapping = self._get_color_data(ds, ranges, dict(style),
                                               factors=factors, colors=colors)
        if 'color' not in cmapping:
            return

        # Enable legend if colormapper is categorical
        cmapper = cmapping['color']['transform']
        if ('color' in cmapping and self.show_legend and
            isinstance(cmapper, CategoricalColorMapper)):
            mapping['legend'] = cdim.name

        if not (self.stacked or self.stack_index) and ds.ndims > 1:
            cmapping.pop('legend', None)
            mapping.pop('legend', None)

        # Merge data and mappings
        mapping.update(cmapping)
        for k, cd in cdata.items():
            if isinstance(cmapper, CategoricalColorMapper) and cd.dtype.kind in 'uif':
                cd = categorize_array(cd, cdim)
            if k not in data or len(data[k]) != [len(data[key]) for key in data if key != k][0]:
                data[k].append(cd)
            else:
                data[k][-1] = cd


    def get_data(self, element, ranges, style):
        if self.stack_index is not None:
            self.param.warning(
                'Bars stack_index plot option is deprecated and will '
                'be ignored, set stacked=True/False instead.')
        if self.group_index not in (None, 1):
            self.param.warning(
                'Bars group_index plot option is deprecated and will '
                'be ignored, set stacked=True/False instead.')

        # Get x, y, group, stack and color dimensions
        group_dim, stack_dim = None, None
        if element.ndims == 1:
            grouping = None
        elif self.stacked or self.stack_index:
            grouping = 'stacked'
            stack_dim = element.get_dimension(1)
        else:
            grouping = 'grouped'
            group_dim = element.get_dimension(1)

        xdim = element.get_dimension(0)
        ydim = element.vdims[0]
        no_cidx = self.color_index is None
        color_index = (group_dim or stack_dim) if no_cidx else self.color_index
        color_dim = element.get_dimension(color_index)
        if color_dim:
            self.color_index = color_dim.name

        # Define style information
        width = style.get('bar_width', style.get('width', 1))
        if 'width' in style:
            self.param.warning("BarPlot width option is deprecated "
                               "use 'bar_width' instead.")
        cmap = style.get('cmap')
        hover = 'hover' in self.handles

        # Group by stack or group dim if necessary
        if group_dim is None:
            grouped = {0: element}
        else:
            grouped = element.groupby(group_dim, group_type=Dataset,
                                      container_type=OrderedDict,
                                      datatype=['dataframe', 'dictionary'])

        y0, y1 = ranges.get(ydim.name, {'combined': (None, None)})['combined']
        if self.logy:
            bottom = (ydim.range[0] or (10**(np.log10(y1)-2)) if y1 else 0.01)
        else:
            bottom = 0
        # Map attributes to data
        if grouping == 'stacked':
            mapping = {'x': xdim.name, 'top': 'top',
                       'bottom': 'bottom', 'width': width}
        elif grouping == 'grouped':
            mapping = {'x': 'xoffsets', 'top': ydim.name, 'bottom': bottom,
                       'width': width}
        else:
            mapping = {'x': xdim.name, 'top': ydim.name, 'bottom': bottom, 'width': width}

        # Get colors
        cdim = color_dim or group_dim
        style_mapping = [v for k, v in style.items() if 'color' in k and
                         (isinstance(v, dim) or v in element)]
        if style_mapping and not no_cidx and self.color_index is not None:
            self.warning("Cannot declare style mapping for '%s' option "
                         "and declare a color_index; ignoring the color_index."
                         % style_mapping[0])
            cdim = None

        cvals = element.dimension_values(cdim, expanded=False) if cdim else None
        if cvals is not None:
            if cvals.dtype.kind in 'uif' and no_cidx:
                cvals = categorize_array(cvals, color_dim)

            factors = None if cvals.dtype.kind in 'uif' else list(cvals)
            if cdim is xdim and factors:
                factors = list(categorize_array(factors, xdim))
            if cmap is None and factors:
                styles = self.style.max_cycles(len(factors))
                colors = [styles[i]['color'] for i in range(len(factors))]
                colors = [rgb2hex(c) if isinstance(c, tuple) else c for c in colors]
            else:
                colors = None
        else:
            factors, colors = None, None

        # Iterate over stacks and groups and accumulate data
        data = defaultdict(list)
        baselines = defaultdict(lambda: {'positive': bottom, 'negative': 0})
        for i, (k, ds) in enumerate(grouped.items()):
            k = k[0] if isinstance(k, tuple) else k
            if group_dim:
                gval = k if isinstance(k, basestring) else group_dim.pprint_value(k)
            # Apply stacking or grouping
            if grouping == 'stacked':
                for sign, slc in [('negative', (None, 0)), ('positive', (0, None))]:
                    slc_ds = ds.select(**{ds.vdims[0].name: slc})
                    xs = slc_ds.dimension_values(xdim)
                    ys = slc_ds.dimension_values(ydim)
                    bs, ts = self.get_stack(xs, ys, baselines, sign)
                    data['bottom'].append(bs)
                    data['top'].append(ts)
                    data[xdim.name].append(xs)
                    data[stack_dim.name].append(slc_ds.dimension_values(stack_dim))
                    if hover: data[ydim.name].append(ys)
                    if not style_mapping:
                        self._add_color_data(slc_ds, ranges, style, cdim, data,
                                             mapping, factors, colors)
            elif grouping == 'grouped':
                xs = ds.dimension_values(xdim)
                ys = ds.dimension_values(ydim)
                xoffsets = [(x if xs.dtype.kind in 'SU' else xdim.pprint_value(x), gval)
                            for x in xs]
                data['xoffsets'].append(xoffsets)
                data[ydim.name].append(ys)
                if hover: data[xdim.name].append(xs)
                if group_dim not in ds.dimensions():
                    ds = ds.add_dimension(group_dim.name, ds.ndims, gval)
                data[group_dim.name].append(ds.dimension_values(group_dim))
            else:
                data[xdim.name].append(ds.dimension_values(xdim))
                data[ydim.name].append(ds.dimension_values(ydim))

            if hover:
                for vd in ds.vdims[1:]:
                    data[vd.name].append(ds.dimension_values(vd))

            if grouping != 'stacked' and not style_mapping:
                self._add_color_data(ds, ranges, style, cdim, data,
                                     mapping, factors, colors)

        # Concatenate the stacks or groups
        sanitized_data = {}
        for col, vals in data.items():
            if len(vals) == 1:
                sanitized_data[dimension_sanitizer(col)] = vals[0]
            elif vals:
                sanitized_data[dimension_sanitizer(col)] = np.concatenate(vals)

        for name, val in mapping.items():
            sanitized = None
            if isinstance(val, basestring):
                sanitized = dimension_sanitizer(mapping[name])
                mapping[name] = sanitized
            elif isinstance(val, dict) and 'field' in val:
                sanitized = dimension_sanitizer(val['field'])
                val['field'] = sanitized
            if sanitized is not None and sanitized not in sanitized_data:
                sanitized_data[sanitized] = []

        # Ensure x-values are categorical
        xname = dimension_sanitizer(xdim.name)
        if xname in sanitized_data:
            sanitized_data[xname] = categorize_array(sanitized_data[xname], xdim)

        # If axes inverted change mapping to match hbar signature
        if self.invert_axes:
            mapping.update({'y': mapping.pop('x'), 'left': mapping.pop('bottom'),
                            'right': mapping.pop('top'), 'height': mapping.pop('width')})

        return sanitized_data, mapping, style
