import re

from mapproxy.response import Response
from mapproxy.exception import RequestError, PlainExceptionHandler
from mapproxy.service.base import Server
from mapproxy.request.tile import TileRequest
from mapproxy.srs import SRS
from mapproxy.config import base_config

from mapproxy.template import template_loader, bunch
get_template = template_loader(__file__, 'templates')

class KMLRequest(TileRequest):
    """
    Class for TMS-like KML requests.
    """
    request_handler_name = 'map'
    req_prefix = '/kml'
    tile_req_re = re.compile(r'''^(?P<begin>/kml)/
            (?P<layer>[^/]+)/
            ((?P<layer_spec>[^/]+)/)?
            (?P<z>-?\d+)/
            (?P<x>-?\d+)/
            (?P<y>-?\d+)\.(?P<format>\w+)''', re.VERBOSE)
    
    def __init__(self, request):
        TileRequest.__init__(self, request)
        if self.format == 'kml':
            self.request_handler_name = 'kml'
    
    @property
    def exception_handler(self):
        return PlainExceptionHandler()

def kml_request(req):
    return KMLRequest(req)

class KMLServer(Server):
    """
    OGC KML 2.2 Server 
    """
    names = ('kml',)
    request_parser = staticmethod(kml_request)
    request_methods = ('map', 'kml')
    template_file = 'kml.xml'
    
    def __init__(self, layers, md):
        Server.__init__(self)
        self.layers = layers
        self.md = md
        
        self.max_age = base_config().tiles.expires_hours * 60 * 60
    
    def map(self, map_request):
        """
        :return: the requested tile
        """
        layer = self.layer(map_request)
        self.authorize_tile_layer(layer.name, map_request.http.environ)
        tile = layer.render(map_request)
        resp = Response(tile.as_buffer(),
                        content_type='image/' + map_request.format)
        resp.cache_headers(tile.timestamp, etag_data=(tile.timestamp, tile.size),
                           max_age=base_config().tiles.expires_hours * 60 * 60)
        resp.make_conditional(map_request.http)
        return resp
    
    def authorize_tile_layer(self, layer_name, env):
        if 'mapproxy.authorize' in env:
            result = env['mapproxy.authorize']('kml', [layer_name])
            if result['authorized'] == 'full':
                return
            if result['authorized'] == 'partial':
                if result['layers'].get(layer_name, {}).get('tile', False) == True:
                    return
            raise RequestError('forbidden', status=403)
    
    def layer(self, tile_request):
        if tile_request.layer in self.layers:
            return self.layers[tile_request.layer]
        if tile_request.layer + '_EPSG4326' in self.layers:
            return self.layers[tile_request.layer + '_EPSG4326']
        if tile_request.layer + '_EPSG900913' in self.layers:
            return self.layers[tile_request.layer + '_EPSG900913']
        raise RequestError('unknown layer: ' + tile_request.layer, request=tile_request)

    def kml(self, map_request):
        """
        :return: the rendered KML response
        """
        layer = self.layer(map_request)
        self.authorize_tile_layer(layer.name, map_request.http.environ)
        
        tile_coord = map_request.tile
        
        initial_level = False
        if tile_coord[2] == 0:
            initial_level = True
        
        bbox = self._tile_wgs_bbox(tile_coord, layer.grid)
        if bbox is None:
            raise RequestError('The requested tile is outside the bounding box '
                               'of the tile map.', request=map_request)
        tile = SubTile(tile_coord, bbox)
        
        subtile_grid, subtiles = self._get_subtiles(tile_coord, layer)
        tile_size = layer.grid.tile_size[0]
        layer = bunch(name=map_request.layer, format=layer.format, md=layer.md)
        service = bunch(url=map_request.http.script_url.rstrip('/'))
        template = get_template(self.template_file)
        result = template.substitute(tile=tile, subtiles=subtiles, layer=layer,
                                 service=service, initial_level=initial_level,
                                 subtile_grid=subtile_grid, tile_size=tile_size)
        resp = Response(result, content_type='application/vnd.google-earth.kml+xml')
        resp.cache_headers(etag_data=(result,), max_age=self.max_age)
        resp.make_conditional(map_request.http)
        return resp

    def _get_subtiles(self, tile, layer):
        """
        Create four `SubTile` for the next level of `tile`.
        """
        bbox = self._tile_bbox(tile, layer.grid)
        bbox_, tile_grid, tiles = layer.grid.get_affected_level_tiles(bbox, tile[2]+1)
        subtiles = []
        for coord in tiles:
            if coord is None: continue
            sub_bbox = self._tile_bbox(coord, layer.grid)
            if sub_bbox is not None:
                # only add subtiles where the lower left corner is in the bbox
                # to prevent subtiles to apear in multiple KML docs
                if sub_bbox[0] >= bbox[0] and sub_bbox[1] >= bbox[1]:
                    sub_bbox_wgs = self._tile_bbox_to_wgs(sub_bbox, layer.grid)
                    subtiles.append(SubTile(coord, sub_bbox_wgs))

        return tile_grid, subtiles

    def _tile_bbox(self, tile_coord, grid):
        tile_coord = grid.internal_tile_coord(tile_coord, use_profiles=False)
        if tile_coord is None:
            return None
        return grid.tile_bbox(tile_coord)
    
    def _tile_wgs_bbox(self, tile_coord, grid):
        src_bbox = self._tile_bbox(tile_coord, grid)
        if src_bbox is None:
            return None
        return self._tile_bbox_to_wgs(src_bbox, grid)
        
    def _tile_bbox_to_wgs(self, src_bbox, grid):
        bbox = grid.srs.transform_bbox_to(SRS(4326), src_bbox, with_points=4)
        if grid.srs == SRS(900913):
            bbox = list(bbox)
            if abs(src_bbox[1] -  -20037508.342789244) < 0.1:
                bbox[1] = -90.0
            if abs(src_bbox[3] -  20037508.342789244) < 0.1:
                bbox[3] = 90.0
        return bbox
    
    def check_map_request(self, map_request):
        if map_request.layer not in self.layers:
            raise RequestError('unknown layer: ' + map_request.layer, request=map_request)


class SubTile(object):
    """
    Contains the ``bbox`` and ``coord`` of a sub tile.
    """
    def __init__(self, coord, bbox):
        self.coord = coord
        self.bbox = bbox