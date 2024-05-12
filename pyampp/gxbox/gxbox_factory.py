import itertools
import astropy.units as u
import astropy.time
import numpy as np
import matplotlib.pyplot as plt
from sunpy.map import Map, make_fitswcs_header, all_pixel_indices_from_map
import sunpy.sun.constants
from astropy.coordinates import SkyCoord
from sunpy.coordinates import Heliocentric, Helioprojective, get_earth, HeliographicStonyhurst, HeliographicCarrington
from datetime import datetime, timedelta
import os
import glob
from pyampp.util.config import *
from pyampp.data import downloader
from pyampp.gxbox.boxutils import hmi_disambig, hmi_b2ptr
import sys
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout,QHBoxLayout, QWidget, QComboBox, QLabel
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar



## todo rsun need to be unified across the code. Ask Gelu to provide a value for rsun.
class Box:
    '''
    Represents a 3D box defined by its origin and dimensions. It calculates and stores the coordinates of the box's edges, distinguishing between bottom edges and other edges.
    '''

    def __init__(self, frame_obs, box_origin, box_center, box_dims, box_res):
        '''
        Initializes the Box instance with origin, dimensions, and computes the corners and edges.

        :param box_center: SkyCoord, the origin point of the box in a given coordinate frame.
        :param box_dims: u.Quantity, the dimensions of the box (x, y, z) in specified units. x and y are in the solar frame, z is the height above the solar surface.
        '''
        self._frame_obs = frame_obs
        self._box_origin = box_origin
        self._box_center = box_center
        self._box_dims = box_dims
        self._box_res = box_res
        # Generate corner points based on the dimensions
        self.corners = list(itertools.product(self._box_dims[0] / 2 * [-1, 1],
                                              self._box_dims[1] / 2 * [-1, 1],
                                              self._box_dims[2] / 2 * [-1, 1]))

        # Identify edges as pairs of corners differing by exactly one dimension
        self.edges = [edge for edge in itertools.combinations(self.corners, 2)
                      if np.count_nonzero(u.Quantity(edge[0]) - u.Quantity(edge[1])) == 1]
        # Initialize properties to store categorized edges
        self._bottom_edges = None
        self._non_bottom_edges = None
        self._calculate_edge_types()  # Categorize edges upon initialization

    def _get_edge_coords(self, edges, box_origin):
        '''
        Translates edge corner points to their corresponding SkyCoord based on the box's origin.

        :param edges: list of tuples, each tuple contains two corner points defining an edge.
        :return: list of SkyCoord, coordinates of edges in the box's frame.
        '''
        return [SkyCoord(x=box_origin.x + u.Quantity([edge[0][0], edge[1][0]]),
                         y=box_origin.y + u.Quantity([edge[0][1], edge[1][1]]),
                         z=box_origin.z + u.Quantity([edge[0][2], edge[1][2]]),
                         frame=box_origin.frame) for edge in edges]

    def _get_bottom_cea_header(self):
        origin = self._box_origin.transform_to(HeliographicStonyhurst)
        shape = self._box_dims[:-1][::-1] / self._box_res.to(self._box_dims.unit)
        shape = list(shape.value)
        shape = [int(np.ceil(s)) for s in shape]
        rsun = origin.rsun.to(self._box_res.unit)
        scale = np.arcsin(self._box_res / rsun).to(u.deg) / u.pix
        scale = u.Quantity((scale, scale))
        bottom_cea_header = make_fitswcs_header(shape, origin,
                                                scale=scale, projection_code='CEA')
        return bottom_cea_header

    def _calculate_edge_types(self):
        '''
        Separates the box's edges into bottom edges and non-bottom edges. This is done in a single pass to improve efficiency.
        '''
        min_z = min(corner[2] for corner in self.corners)
        bottom_edges, non_bottom_edges = [], []
        for edge in self.edges:
            if edge[0][2] == min_z and edge[1][2] == min_z:
                bottom_edges.append(edge)
            else:
                non_bottom_edges.append(edge)
        self._bottom_edges = self._get_edge_coords(bottom_edges, self._box_center)
        self._non_bottom_edges = self._get_edge_coords(non_bottom_edges, self._box_center)

    def _get_bounds_coords(self, edges, bltr=False, pad=False):

        '''
        Provides the bounding box of the edges in solar x and y.

        :param edges: list of tuples, each tuple contains two corner points defining an edge.
        :type edges: list
        :param bltr: boolean, if True, returns bottom left and top right coordinates, otherwise returns minimum and maximum coordinates.
        :type bltr: bool, optional
        :param pad: boolean, if True, adds padding to the bounding box.
        :type pad: bool, optional

        :return: tuple of SkyCoord, the coordinates of the box's bounds.
        :rtype: list of SkyCoord
        '''
        xx = []
        yy = []
        for edge in edges:
            xx.append(edge.transform_to(self._frame_obs).Tx)
            yy.append(edge.transform_to(self._frame_obs).Ty)
        unit = xx[0][0].unit
        if bltr:
            min_x = np.min(xx)
            max_x = np.max(xx)
            min_y = np.min(yy)
            max_y = np.max(yy)
            if pad:
                _pad = 0.3 * np.max([max_x - min_x, max_y - min_y, 20])
                min_x -= _pad
                max_x += _pad
                min_y -= _pad
                max_y += _pad
            bottom_left = SkyCoord(min_x * unit, min_y * unit, frame=self._frame_obs)
            top_right = SkyCoord(max_x * unit, max_y * unit, frame=self._frame_obs)
            return [bottom_left, top_right]
        else:
            coords = SkyCoord(Tx=[np.min(xx), np.max(xx)] * unit, Ty=[np.min(yy), np.max(yy)] * unit,
                              frame=self._frame_obs)
            return coords

    @property
    def bl_tr_pad_coords(self):
        '''
        Provides access to the box's bottom left and top right bounds in the observer frame.

        :return: list of SkyCoord, the bounds of the box's bottom edges in the observer frame.
        '''
        return self._get_bounds_coords(self.all_edges, bltr=True, pad=True)

    @property
    def bl_tr_coords(self):
        '''
        Provides access to the box's bottom left and top right bounds in the observer frame.

        :return: list of SkyCoord, the bounds of the box's bottom edges in the observer frame.
        '''
        return self._get_bounds_coords(self.all_edges, bltr=True)

    @property
    def bounds_coords(self):
        '''
        Provides access to the box's bottom bounds in the observer frame.

        :return: SkyCoord, the bounds of the box's bottom edges in the observer frame.
        '''
        return self._get_bounds_coords(self.all_edges)

    @property
    def bottom_bounds_coords(self):
        '''
        Provides access to the box's bottom bounds in the observer frame.

        :return: SkyCoord, the bounds of the box's bottom edges in the observer frame.
        '''
        return self._get_bounds_coords(self.bottom_edges)

    @property
    def bottom_cea_header(self):
        '''
        Provides access to the box's bottom WCS CEA header.

        :return: WCS CEA header, the WCS CEA header for the box's bottom.
        '''
        return self._get_bottom_cea_header()

    @property
    def bottom_edges(self):
        '''
        Provides access to the box's bottom edge coordinates.

        :return: list of SkyCoord, coordinates of the box's bottom edges.
        '''
        return self._bottom_edges

    @property
    def non_bottom_edges(self):
        '''
        Provides access to the box's non-bottom edge coordinates.

        :return: list of SkyCoord, coordinates of the box's non-bottom edges.
        '''
        return self._non_bottom_edges

    @property
    def all_edges(self):
        '''
        Provides access to all the edge coordinates of the box, combining both bottom and non-bottom edges.

        :return: list of SkyCoord, coordinates of all the edges of the box.
        '''
        return self._bottom_edges + self._non_bottom_edges

    @property
    def box_origin(self):
        '''
        Provides read-only access to the box's origin coordinates.

        :return: SkyCoord, the origin of the box in the specified frame.
        '''
        return self._box_center

    @property
    def box_dims(self):
        '''
        Provides read-only access to the box's dimensions.

        :return: u.Quantity, the dimensions of the box (length, width, height) in specified units.
        '''
        return self._box_dims


class GxBox(QMainWindow):
    def __init__(self, time, observer, box_origin, box_dimensions=u.Quantity([100, 100, 100]) * u.Mm,
                 box_res=1.4 * u.Mm):
        """
        Initialize the GxBox object

        :param time: Observation time.
        :type time: astropy.time.Time
        :param observer: Observer location.
        :type observer: astropy.coordinates.SkyCoord
        :param box_origin: The origin of the box (center of the box bottom).
        :type box_origin: astropy.coordinates.SkyCoord
        :param box_dimensions: Dimensions of the box in heliocentric coordinates, defaults to 100x100x100 Mm.
        :type box_dimensions: u.Quantity
        :param box_res: Spatial resolution of the box, defaults to 1.4 Mm.
        :type box_res: u.Quantity
        """
        super(GxBox, self).__init__()
        self.time = time
        self.observer = observer
        self.box_dimensions = box_dimensions
        self.box_res = box_res
        ## this is the origin of the box, i.e., the center of the box bottom
        self.box_origin = box_origin
        self.sdofitsfiles = None
        self.frame_hcc = Heliocentric(observer=self.box_origin, obstime=self.time)
        self.frame_obs = Helioprojective(observer=self.observer, obstime=self.time)
        self.lines_of_sight = []
        self.edge_coords = []
        self.axes = None
        self.fig = None
        self.init_map_context_name = '171'
        self.init_map_bottom_name = 'br'

        ## this is a dummy map. it should be replaced by a real map from inputs.
        self.instrument_map = self.make_dummy_map(self.box_origin.transform_to(self.frame_obs))

        box_center = box_origin.transform_to(self.frame_hcc)
        box_center = SkyCoord(x=box_center.x,
                              y=box_center.y,
                              z=box_center.z + box_dimensions[2] / 2,
                              frame=box_center.frame)
        ## this is the center of the box
        self.box_center = box_center

        self.define_simbox(self.frame_obs, self.box_origin, self.box_center, self.box_dimensions, self.box_res)
        self.box_bounds = self.simbox.bounds_coords

        download_sdo = downloader.SDOImageDownloader(time)
        self.sdofitsfiles = download_sdo.download_images()
        self.sdomaps = {}

        self.bottom_wcs_header = self.simbox.bottom_cea_header
        self.fov_coords = self.simbox.bl_tr_pad_coords
        self.sdomaps[self.init_map_context_name] = self.loadmap(self.init_map_context_name)
        self.map_context = self.sdomaps[self.init_map_context_name]
        self.bottom_wcs_header['rsun_ref'] = self.map_context.meta['rsun_ref']
        self.sdomaps[self.init_map_bottom_name] = self.loadmap(self.init_map_bottom_name)

        self.map_bottom = self.sdomaps[self.init_map_bottom_name].reproject_to(self.bottom_wcs_header, algorithm="adaptive",
                                                          roundtrip_coords=False)
        self.init_ui()

    @property
    def avaliable_maps(self):
        """
        Lists the available maps.

        :return: A list of available map keys.
        :rtype: list
        """
        if all(key in self.sdofitsfiles.keys() for key in HMI_B_SEGMENTS):
            return list(self.sdofitsfiles.keys()) + HMI_B_PRODUCTS
        else:
            return self.sdofitsfiles.keys()

    def _load_hmi_b_seg_maps(self, mapname, fov_coords):
        """
        Load specific HMI B segment maps required for the magnetic field vector data products.

        :param mapname: Name of the map to load.
        :type mapname: str
        :param fov_coords: The field of view coordinates (bottom left and top right) as SkyCoord objects.
        :type fov_coords: list
        :return: Loaded map object.
        :rtype: sunpy.map.Map
        :raises ValueError: If the map name is not in the expected HMI B segments.
        """
        if mapname not in HMI_B_SEGMENTS:
            raise ValueError(f"mapname: {mapname} must be one of {HMI_B_SEGMENTS}. Use loadmap method for others.")

        if mapname in self.sdomaps.keys():
            return self.sdomaps[mapname]

        loaded_map = Map(self.sdofitsfiles[mapname]).submap(fov_coords[0], top_right=fov_coords[1])
        # loaded_map = loaded_map.rotate(order=3)
        if mapname in ['azimuth']:
            if 'disambig' not in self.sdomaps.keys():
                self.sdomaps['disambig'] = Map(self.sdofitsfiles['disambig']).submap(fov_coords[0],
                                                                                     top_right=fov_coords[1])
            loaded_map = hmi_disambig(loaded_map, self.sdomaps['disambig'])

        self.sdomaps[mapname] = loaded_map
        return loaded_map

    def loadmap(self, mapname, fov_coords=None):
        """
        Loads a map from the available data.

        :param mapname: Name of the map to load.
        :type mapname: str
        :param fov_coords: Field of view coordinates (bottom left and top right) as SkyCoord objects, optional. Defaults to the entire FOV if not specified.
        :type fov_coords: list, optional
        :return: The requested map.
        :raises ValueError: If the specified map is not available.
        """
        if mapname not in self.avaliable_maps:
            raise ValueError(f"Map {mapname} is not available. mapname must be one of {self.avaliable_maps}")

        if mapname in self.sdomaps.keys():
            return self.sdomaps[mapname]

        if fov_coords is None:
            fov_coords = self.fov_coords

        if mapname in HMI_B_SEGMENTS:
            self._load_hmi_b_seg_maps(mapname, fov_coords)

        if mapname in HMI_B_PRODUCTS:
            for key in HMI_B_SEGMENTS:
                if key not in self.sdomaps.keys():
                    self.sdomaps[key] = self._load_hmi_b_seg_maps(key, fov_coords)
            map_bp, map_bt, map_br = hmi_b2ptr(self.sdomaps['field'], self.sdomaps['inclination'],
                                               self.sdomaps['azimuth'])
            self.sdomaps['bp'] = map_bp
            self.sdomaps['bt'] = map_bt
            self.sdomaps['br'] = map_br
            return self.sdomaps[mapname]

        # Load general maps
        self.sdomaps[mapname] = Map(self.sdofitsfiles[mapname]).submap(fov_coords[0], top_right=fov_coords[1])
        return self.sdomaps[mapname]

    def make_dummy_map(self, ref_coord):
        instrument_data = np.nan * np.ones((50, 50))
        instrument_header = make_fitswcs_header(instrument_data,
                                                ref_coord,
                                                scale=u.Quantity([10, 10]) * u.arcsec / u.pix)
        return Map(instrument_data, instrument_header)

    def define_simbox(self, frame_obs, box_origin, box_center, box_dimensions, box_res):
        # Finally, we can define the edge coordinates of the box by first creating a coordinate to represent the origin. This is easily computed from our point that defined the orientation since this is the point at which the box is tangent to the solar surface.

        # In[11]:

        # Using that origin, we can compute the coordinates of all edges.

        self.simbox = Box(frame_obs, box_origin, box_center, box_dimensions, box_res)

    def init_ui(self):
        self.setWindowTitle('GxBox Map Viewer')
        # self.setGeometry(100, 100, 800, 600)
        # Create a central widget
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)

        # Layout
        main_layout = QVBoxLayout(central_widget)

        # Horizontal layout for dropdowns and labels
        dropdown_layout = QHBoxLayout()

        # Dropdown for bottom map selection
        self.map_bottom_selector = QComboBox()
        self.map_bottom_selector.addItems(list(self.avaliable_maps))
        self.map_bottom_selector.setCurrentIndex(self.avaliable_maps.index(self.init_map_bottom_name))
        self.map_bottom_selector_label = QLabel("Select Bottom Map:")
        dropdown_layout.addWidget(self.map_bottom_selector_label)
        dropdown_layout.addWidget(self.map_bottom_selector)

        # Dropdown for context map selection
        self.map_context_selector = QComboBox()
        self.map_context_selector.addItems(list(self.avaliable_maps))
        self.map_context_selector.setCurrentIndex(self.avaliable_maps.index(self.init_map_context_name))
        self.map_context_selector_label = QLabel("Select Context Map:")
        dropdown_layout.addWidget(self.map_context_selector_label)
        dropdown_layout.addWidget(self.map_context_selector)

        main_layout.addLayout(dropdown_layout)

        # Connect dropdowns to their respective handlers
        self.map_bottom_selector.currentTextChanged.connect(self.update_bottom_map)
        self.map_context_selector.currentTextChanged.connect(self.update_context_map)


        # Matplotlib Figure
        self.fig = plt.Figure()
        self.canvas = FigureCanvas(self.fig)
        main_layout.addWidget(self.canvas)

        # Add Matplotlib Navigation Toolbar
        self.toolbar = NavigationToolbar(self.canvas, self)
        main_layout.addWidget(self.toolbar)

        self.update_plot()

        map_context_aspect_ratio = (self.map_context.dimensions[1] / self.map_context.dimensions[0]).value
        window_width = 800
        window_height = int(window_width * map_context_aspect_ratio)

        # Adjust for padding, toolbar, and potential high DPI scaling
        window_width += 0  # Adjust based on your UI needs
        window_height += 150  # Includes space for toolbar and dropdowns

        self.setGeometry(100, 100, int(window_width), int(window_height))

    def update_bottom_map(self, map_name):
        map_bottom = self.sdomaps[map_name] if map_name in self.sdomaps.keys() else self.loadmap(map_name)
        self.map_bottom = map_bottom.reproject_to(self.bottom_wcs_header, algorithm="adaptive",
                                                          roundtrip_coords=False)
        self.update_plot()

    def update_context_map(self, map_name):
        self.map_context = self.sdomaps[map_name] if map_name in self.sdomaps.keys() else self.loadmap(map_name)
        self.update_plot()

    def update_plot(self):
            self.fig.clear()
            self.axes = self.fig.add_subplot(projection=self.map_context)
            ax = self.axes
            self.map_context.plot(axes=ax, cmap='gray')
            self.map_context.draw_grid(axes=ax, color='w', lw=0.5)
            self.map_context.draw_limb(axes=ax, color='w', lw=1.0)
            # for edge in self.simbox.bottom_edges:
            #     ax.plot_coord(edge, color='r', ls='-', marker='', lw=1.0)
            # for edge in self.simbox.non_bottom_edges:
            #     ax.plot_coord(edge, color='r', ls='--', marker='', lw=0.5)
            for edge in self.simbox.bottom_edges:
                ax.plot_coord(edge, color='tab:red', ls='--', marker='', lw=0.5)
            for edge in self.simbox.non_bottom_edges:
                ax.plot_coord(edge, color='tab:red', ls='-', marker='', lw=1.0)
            # ax.plot_coord(self.box_center, color='r', marker='+')
            # ax.plot_coord(self.box_origin, mec='r', mfc='none', marker='o')
            self.map_context.draw_quadrangle(
                self.simbox.bounds_coords,
                axes=ax,
                edgecolor="tab:blue",
                linestyle="--",
                linewidth=0.5,
            )
            self.map_bottom.plot(axes=ax, autoalign=True)
            ax.set_title(ax.get_title(), pad=45)
            self.fig.tight_layout()
            # Refresh canvas
            self.canvas.draw()

    def create_lines_of_sight(self):
        # The rest of the code for creating lines of sight goes here
        pass

    def visualize(self):
        # The rest of the code for visualization goes here
        pass

if __name__ == '__main__':
    import astropy.time
    import sunpy.sun.constants
    from astropy.coordinates import SkyCoord
    from sunpy.coordinates import Heliocentric, Helioprojective, get_earth
    import astropy.units as u
    from pyampp.gxbox.gxbox_factory import GxBox

    # time = astropy.time.Time('2024-05-09T17:12:00')
    # box_origin = SkyCoord(450 * u.arcsec, -256 * u.arcsec, obstime=time, observer="earth", frame='helioprojective')
    time = astropy.time.Time('2014-11-01T16:40:00')
    # box_origin = SkyCoord(lon=30 * u.deg, lat=20 * u.deg,
    #                       radius=sunpy.sun.constants.radius,
    #                       frame='heliographic_stonyhurst')
    ## dots source
    # box_origin = SkyCoord(-475 * u.arcsec, -330 * u.arcsec, obstime=time, observer="earth", frame='helioprojective')
    ## flare AR
    box_origin = SkyCoord(-632 * u.arcsec, -135 * u.arcsec, obstime=time, observer="earth", frame='helioprojective')
    observer = get_earth(time)
    box_dimensions = u.Quantity([150, 150, 100]) * u.Mm
    # box_dimensions = u.Quantity([200, 200, 200]) * u.Mm
    box_res = 0.6 * u.Mm

    app = QApplication(sys.argv)
    gxbox = GxBox(time, observer, box_origin, box_dimensions,box_res)
    gxbox.show()

    # sys.exit(app.exec_())
