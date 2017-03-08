
"""
Utility methods for working with WSGI servers
"""
import eventlet
import sys

import oslo_i18n as i18n
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_service import service as common_service
from oslo_utils import excutils
import routes.middleware
import six
import webob.dec
import webob.exc

from hypernet_agentless._i18n import _
from hypernet_agentless.common import exceptions
from hypernet_agentless.server import context
from hypernet_agentless.server.db import api


LOG = logging.getLogger(__name__)


class WorkerService(common_service.ServiceBase):
    """Wraps a worker to be handled by ProcessLauncher."""

    def __init__(self, service, application):
        self._service = service
        self._application = application
        self._server = None

    def start(self):
        # We may have just forked from parent process.  A quick disposal of the
        # existing sql connections avoids producting 500 errors later when they
        # are discovered to be broken.
        api.get_engine().pool.dispose()
        self._server = self._service.pool.spawn(self._service._run,
                                                self._application,
                                                self._service._socket)

    def wait(self):
        self._service.pool.waitall()

    def stop(self):
        if isinstance(self._server, eventlet.greenthread.GreenThread):
            self._server.kill()
            self._server = None

    def reset(self):
        pass


class Middleware(object):
    """Base WSGI middleware wrapper.
    These classes require an application to be initialized that will be called
    next.  By default the middleware will simply call its wrapped app, or you
    can override __call__ to customize its behavior.
    """

    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.
        Any local configuration (that is, values under the [filter:APPNAME]
        section of the paste config) will be passed into the `__init__` method
        as kwargs.
        A hypothetical configuration would look like:
            [filter:analytics]
            redis_host = 127.0.0.1
            paste.filter_factory = nova.api.analytics:Analytics.factory
        which would result in a call to the `Analytics` class as
            import nova.api.analytics
            analytics.Analytics(app_from_paste, redis_host='127.0.0.1')
        You could of course re-implement the `factory` method in subclasses,
        but using the kwarg passing it shouldn't be necessary.
        """
        def _factory(app):
            return cls(app, **local_config)
        return _factory

    def __init__(self, application):
        self.application = application

    def process_request(self, req):
        """Called on each request.
        If this returns None, the next application down the stack will be
        executed. If it returns a response then that response will be returned
        and execution will stop here.
        """
        return None

    def process_response(self, response):
        """Do whatever you'd like to the response."""
        return response

    @webob.dec.wsgify
    def __call__(self, req):
        response = self.process_request(req)
        if response:
            return response
        response = req.get_response(self.application)
        return self.process_response(response)


class Request(webob.Request):

    def best_match_content_type(self):
        """Determine the most acceptable content-type.
        Based on:
            1) URI extension (.json)
            2) Content-type header
            3) Accept* headers
        """
        # First lookup http request path
        parts = self.path.rsplit('.', 1)
        if len(parts) > 1:
            _format = parts[1]
            if _format in ['json']:
                return 'application/{0}'.format(_format)

        # Then look up content header
        type_from_header = self.get_content_type()
        if type_from_header:
            return type_from_header
        ctypes = ['application/json']

        # Finally search in Accept-* headers
        bm = self.accept.best_match(ctypes)
        return bm or 'application/json'

    def get_content_type(self):
        allowed_types = ("application/json")
        if "Content-Type" not in self.headers:
            LOG.debug(_("Missing Content-Type"))
            return None
        _type = self.content_type
        if _type in allowed_types:
            return _type
        return None

    def best_match_language(self):
        """Determines best available locale from the Accept-Language header.
        :returns: the best language match or None if the 'Accept-Language'
                  header was not available in the request.
        """
        if not self.accept_language:
            return None
        all_languages = i18n.get_available_languages('hypernet')
        return self.accept_language.best_match(all_languages)

    @property
    def context(self):
        if 'hypernet.context' not in self.environ:
            self.environ['hypernet.context'] = context.get_admin_context()
        return self.environ['hypernet.context']


class ActionDispatcher(object):
    """Maps method name to local methods through action name."""

    def dispatch(self, *args, **kwargs):
        """Find and call local method."""
        action = kwargs.pop('action', 'default')
        action_method = getattr(self, str(action), self.default)
        return action_method(*args, **kwargs)

    def default(self, data):
        raise NotImplementedError()


class DictSerializer(ActionDispatcher):
    """Default request body serialization."""

    def serialize(self, data, action='default'):
        return self.dispatch(data, action=action)

    def default(self, data):
        return ""


class JSONDictSerializer(DictSerializer):
    """Default JSON request body serialization."""

    def default(self, data):
        def sanitizer(obj):
            return six.text_type(obj)
        return jsonutils.dumps(data, default=sanitizer)


class ResponseHeaderSerializer(ActionDispatcher):
    """Default response headers serialization."""

    def serialize(self, response, data, action):
        self.dispatch(response, data, action=action)

    def default(self, response, data):
        response.status_int = 200


class ResponseSerializer(object):
    """Encode the necessary pieces into a response object."""

    def __init__(self, body_serializers=None, headers_serializer=None):
        self.body_serializers = {
            'application/json': JSONDictSerializer(),
        }
        self.body_serializers.update(body_serializers or {})

        self.headers_serializer = (headers_serializer or
                                   ResponseHeaderSerializer())

    def serialize(self, response_data, content_type, action='default'):
        """Serialize a dict into a string and wrap in a wsgi.Request object.
        :param response_data: dict produced by the Controller
        :param content_type: expected mimetype of serialized response body
        """
        response = webob.Response()
        self.serialize_headers(response, response_data, action)
        self.serialize_body(response, response_data, content_type, action)
        return response

    def serialize_headers(self, response, data, action):
        self.headers_serializer.serialize(response, data, action)

    def serialize_body(self, response, data, content_type, action):
        response.headers['Content-Type'] = content_type
        if data is not None:
            serializer = self.get_body_serializer(content_type)
            response.body = serializer.serialize(data, action)

    def get_body_serializer(self, content_type):
        try:
            return self.body_serializers[content_type]
        except (KeyError, TypeError):
            raise exceptions.InvalidContentType(content_type=content_type)


class TextDeserializer(ActionDispatcher):
    """Default request body deserialization."""

    def deserialize(self, datastring, action='default'):
        return self.dispatch(datastring, action=action)

    def default(self, datastring):
        return {}


class JSONDeserializer(TextDeserializer):

    def _from_json(self, datastring):
        try:
            return jsonutils.loads(datastring)
        except ValueError:
            msg = _("Cannot understand JSON")
            LOG.exception(_("InvalidJSON: %s"), msg)
            raise exceptions.MalformedRequestBody(reason=msg)

    def default(self, datastring):
        return {'body': self._from_json(datastring)}


class RequestHeadersDeserializer(ActionDispatcher):
    """Default request headers deserializer."""

    def deserialize(self, request, action):
        return self.dispatch(request, action=action)

    def default(self, request):
        return {}


class RequestDeserializer(object):
    """Break up a Request object into more useful pieces."""

    def __init__(self, body_deserializers=None, headers_deserializer=None):
        self.body_deserializers = {
            'application/json': JSONDeserializer(),
        }
        self.body_deserializers.update(body_deserializers or {})

        self.headers_deserializer = (headers_deserializer or
                                     RequestHeadersDeserializer())

    def deserialize(self, request):
        """Extract necessary pieces of the request.
        :param request: Request object
        :returns tuple of expected controller action name, dictionary of
                 keyword arguments to pass to the controller, the expected
                 content type of the response
        """
        action_args = self.get_action_args(request.environ)
        action = action_args.pop('action', None)

        action_args.update(self.deserialize_headers(request, action))
        action_args.update(self.deserialize_body(request, action))

        accept = self.get_expected_content_type(request)

        return (action, action_args, accept)

    def deserialize_headers(self, request, action):
        return self.headers_deserializer.deserialize(request, action)

    def deserialize_body(self, request, action):
        try:
            content_type = request.best_match_content_type()
        except exceptions.InvalidContentType:
            LOG.debug(_("Unrecognized Content-Type provided in request"))
            return {}

        if content_type is None:
            LOG.debug(_("No Content-Type provided in request"))
            return {}

        if not len(request.body) > 0:
            LOG.debug(_("Empty body provided in request"))
            return {}

        try:
            deserializer = self.get_body_deserializer(content_type)
        except exceptions.InvalidContentType:
            with excutils.save_and_reraise_exception():
                LOG.debug(_("Unable to deserialize body as provided "
                            "Content-Type"))

        return deserializer.deserialize(request.body, action)

    def get_body_deserializer(self, content_type):
        try:
            return self.body_deserializers[content_type]
        except (KeyError, TypeError):
            raise exceptions.InvalidContentType(content_type=content_type)

    def get_expected_content_type(self, request):
        return request.best_match_content_type()

    def get_action_args(self, request_environment):
        """Parse dictionary created by routes library."""
        try:
            args = request_environment['wsgiorg.routing_args'][1].copy()
        except Exception:
            return {}

        try:
            del args['controller']
        except KeyError:
            pass

        try:
            del args['format']
        except KeyError:
            pass

        return args


class Application(object):
    """Base WSGI application wrapper. Subclasses need to implement __call__."""

    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.
        Any local configuration (that is, values under the [app:APPNAME]
        section of the paste config) will be passed into the `__init__` method
        as kwargs.
        A hypothetical configuration would look like:
            [app:wadl]
            latest_version = 1.3
            paste.app_factory = nova.api.fancy_api:Wadl.factory
        which would result in a call to the `Wadl` class as
            import hypernet.api.fancy_api
            fancy_api.Wadl(latest_version='1.3')
        You could of course re-implement the `factory` method in subclasses,
        but using the kwarg passing it shouldn't be necessary.
        """
        return cls(**local_config)

    def __call__(self, environ, start_response):
        r"""Subclasses will probably want to implement __call__ like this:
        @webob.dec.wsgify(RequestClass=Request)
        def __call__(self, req):
          # Any of the following objects work as responses:
          # Option 1: simple string
          res = 'message\n'
          # Option 2: a nicely formatted HTTP exception page
          res = exc.HTTPForbidden(explanation='Nice try')
          # Option 3: a webob Response object (in case you need to play with
          # headers, or you want to be treated like an iterable, or or or)
          res = Response();
          res.app_iter = open('somefile')
          # Option 4: any wsgi app to be run next
          res = self.application
          # Option 5: you can get a Response object for a wsgi app, too, to
          # play with headers etc
          res = req.get_response(self.application)
          # You can then just return your response...
          return res
          # ... or set req.response and return None.
          req.response = res
        See the end of http://pythonpaste.org/webob/modules/dec.html
        for more info.
        """
        raise NotImplementedError(_('You must implement __call__'))


class Debug(Middleware):
    """Middleware for debugging.
    Helper class that can be inserted into any WSGI application chain
    to get information about the request and response.
    """

    @webob.dec.wsgify
    def __call__(self, req):
        print(("*" * 40) + " REQUEST ENVIRON")
        for key, value in req.environ.items():
            print(key, "=", value)
        print()
        resp = req.get_response(self.application)

        print(("*" * 40) + " RESPONSE HEADERS")
        for (key, value) in six.iteritems(resp.headers):
            print(key, "=", value)
        print()

        resp.app_iter = self.print_generator(resp.app_iter)

        return resp

    @staticmethod
    def print_generator(app_iter):
        """Print contents of a wrapper string iterator when iterated."""
        print(("*" * 40) + " BODY")
        for part in app_iter:
            sys.stdout.write(part)
            sys.stdout.flush()
            yield part
        print()


class Router(object):
    """WSGI middleware that maps incoming requests to WSGI apps."""

    @classmethod
    def factory(cls, global_config, **local_config):
        """Return an instance of the WSGI Router class."""
        return cls()

    def __init__(self, mapper):
        """Create a router for the given routes.Mapper.
        Each route in `mapper` must specify a 'controller', which is a
        WSGI app to call.  You'll probably want to specify an 'action' as
        well and have your controller be a wsgi.Controller, who will route
        the request to the action method.
        Examples:
          mapper = routes.Mapper()
          sc = ServerController()
          # Explicit mapping of one route to a controller+action
          mapper.connect(None, "/svrlist", controller=sc, action="list")
          # Actions are all implicitly defined
          mapper.resource("network", "networks", controller=nc)
          # Pointing to an arbitrary WSGI app.  You can specify the
          # {path_info:.*} parameter so the target app can be handed just that
          # section of the URL.
          mapper.connect(None, "/v1.0/{path_info:.*}", controller=BlogApp())
        """
        self.map = mapper
        self._router = routes.middleware.RoutesMiddleware(self._dispatch,
                                                          self.map)

    @webob.dec.wsgify
    def __call__(self, req):
        """Route the incoming request to a controller based on self.map.
        If no match, return a 404.
        """
        return self._router

    @staticmethod
    @webob.dec.wsgify(RequestClass=Request)
    def _dispatch(req):
        """Dispatch a Request.
        Called by self._router after matching the incoming request to a route
        and putting the information into req.environ. Either returns 404
        or the routed WSGI app's response.
        """
        match = req.environ['wsgiorg.routing_args'][1]
        if not match:
            language = req.best_match_language()
            msg = _('The resource could not be found.')
            msg = i18n.translate(msg, language)
            return webob.exc.HTTPNotFound(explanation=msg)
        app = match['controller']
        return app


class Resource(Application):
    """WSGI app that handles (de)serialization and controller dispatch.
    WSGI app that reads routing information supplied by RoutesMiddleware
    and calls the requested action method upon its controller.  All
    controller action methods must accept a 'req' argument, which is the
    incoming wsgi.Request. If the operation is a PUT or POST, the controller
    method must also accept a 'body' argument (the deserialized request body).
    They may raise a webob.exc exception or return a dict, which will be
    serialized by requested content type.
    """

    def __init__(self, controller, fault_body_function,
                 deserializer=None, serializer=None):
        """Object initialization.
        :param controller: object that implement methods created by routes lib
        :param deserializer: object that can serialize the output of a
                             controller into a webob response
        :param serializer: object that can deserialize a webob request
                           into necessary pieces
        :param fault_body_function: a function that will build the response
                                    body for HTTP errors raised by operations
                                    on this resource object
        """
        self.controller = controller
        self.deserializer = deserializer or RequestDeserializer()
        self.serializer = serializer or ResponseSerializer()
        self._fault_body_function = fault_body_function

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, request):
        """WSGI method that controls (de)serialization and method dispatch."""

        LOG.info(_("%(method)s %(url)s"), {"method": request.method,
                                           "url": request.url})

        try:
            action, args, accept = self.deserializer.deserialize(request)
        except exceptions.InvalidContentType:
            msg = _("Unsupported Content-Type")
            LOG.exception(_("InvalidContentType: %s"), msg)
            return Fault(webob.exc.HTTPBadRequest(explanation=msg))
        except exceptions.MalformedRequestBody:
            msg = _("Malformed request body")
            LOG.exception(_("MalformedRequestBody: %s"), msg)
            return Fault(webob.exc.HTTPBadRequest(explanation=msg))

        try:
            action_result = self.dispatch(request, action, args)
        except webob.exc.HTTPException as ex:
            LOG.info(_("HTTP exception thrown: %s"), six.text_type(ex))
            action_result = Fault(ex,
                                  self._fault_body_function)
        except Exception:
            LOG.exception(_("Internal error"))
            # Do not include the traceback to avoid returning it to clients.
            action_result = Fault(webob.exc.HTTPServerError(),
                                  self._fault_body_function)

        if isinstance(action_result, dict) or action_result is None:
            response = self.serializer.serialize(action_result,
                                                 accept,
                                                 action=action)
        else:
            response = action_result

        try:
            msg_dict = dict(url=request.url, status=response.status_int)
            msg = _("%(url)s returned with HTTP %(status)d") % msg_dict
        except AttributeError as e:
            msg_dict = dict(url=request.url, exception=e)
            msg = _("%(url)s returned a fault: %(exception)s") % msg_dict

        LOG.info(msg)

        return response

    def dispatch(self, request, action, action_args):
        """Find action-spefic method on controller and call it."""

        controller_method = getattr(self.controller, action)
        try:
            # NOTE(salvatore-orlando): the controller method must have
            # an argument whose name is 'request'
            return controller_method(request=request, **action_args)
        except TypeError as exc:
            LOG.exception(exc)
            return Fault(webob.exc.HTTPBadRequest())


def _default_body_function(wrapped_exc):
    code = wrapped_exc.status_int
    fault_data = {
        'Error': {
            'code': code,
            'message': wrapped_exc.explanation}}
    # 'code' is an attribute on the fault tag itself
    metadata = {'attributes': {'Error': 'code'}}
    return fault_data, metadata


class Fault(webob.exc.HTTPException):
    """Generates an HTTP response from a webob HTTP exception."""

    def __init__(self, exception, body_function=None):
        """Creates a Fault for the given webob.exc.exception."""
        self.wrapped_exc = exception
        self.status_int = self.wrapped_exc.status_int
        self._body_function = body_function or _default_body_function

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, req):
        """Generate a WSGI response based on the exception passed to ctor."""
        # Replace the body with fault details.
        fault_data, metadata = self._body_function(self.wrapped_exc)
        LOG.info(req)
        content_type = req.best_match_content_type()
        serializer = {
            'application/json': JSONDictSerializer(),
        }[content_type]

        self.wrapped_exc.body = serializer.serialize(fault_data)
        self.wrapped_exc.content_type = content_type
        return self.wrapped_exc


# NOTE(salvatore-orlando): this class will go once the
# extension API framework is updated
class Controller(object):
    """WSGI app that dispatched to methods.
    WSGI app that reads routing information supplied by RoutesMiddleware
    and calls the requested action method upon itself.  All action methods
    must, in addition to their normal parameters, accept a 'req' argument
    which is the incoming wsgi.Request.  They raise a webob.exc exception,
    or return a dict which will be serialized by requested content type.
    """

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, req):
        """Call the method specified in req.environ by RoutesMiddleware."""
        arg_dict = req.environ['wsgiorg.routing_args'][1]
        action = arg_dict['action']
        method = getattr(self, action)
        del arg_dict['controller']
        del arg_dict['action']
        if 'format' in arg_dict:
            del arg_dict['format']
        arg_dict['request'] = req
        result = method(**arg_dict)

        if isinstance(result, dict) or result is None:
            if result is None:
                status = 204
                content_type = ''
                body = None
            else:
                status = 200
                content_type = req.best_match_content_type()
                body = self._serialize(result, content_type)

            response = webob.Response(status=status,
                                      content_type=content_type,
                                      body=body)
            msg_dict = dict(url=req.url, status=response.status_int)
            msg = _("%(url)s returned with HTTP %(status)d") % msg_dict
            LOG.debug(msg)
            return response
        else:
            return result

    def _serialize(self, data, content_type):
        """Serialize the given dict to the provided content_type.
        Uses self._serialization_metadata if it exists, which is a dict mapping
        MIME types to information needed to serialize to that type.
        """
        _metadata = getattr(type(self), '_serialization_metadata', {})

        serializer = Serializer(_metadata)
        try:
            return serializer.serialize(data, content_type)
        except exceptions.InvalidContentType:
            msg = _('The requested content type %s is invalid.') % content_type
            raise webob.exc.HTTPNotAcceptable(msg)

    def _deserialize(self, data, content_type):
        """Deserialize the request body to the specefied content type.
        Uses self._serialization_metadata if it exists, which is a dict mapping
        MIME types to information needed to serialize to that type.
        """
        _metadata = getattr(type(self), '_serialization_metadata', {})
        serializer = Serializer(_metadata)
        return serializer.deserialize(data, content_type)['body']


# NOTE(salvatore-orlando): this class will go once the
# extension API framework is updated
class Serializer(object):
    """Serializes and deserializes dictionaries to certain MIME types."""

    def __init__(self, metadata=None):
        """Create a serializer based on the given WSGI environment.
        'metadata' is an optional dict mapping MIME types to information
        needed to serialize a dictionary to that type.
        """
        self.metadata = metadata or {}

    def _get_serialize_handler(self, content_type):
        handlers = {
            'application/json': JSONDictSerializer(),
        }

        try:
            return handlers[content_type]
        except Exception:
            raise exceptions.InvalidContentType(content_type=content_type)

    def serialize(self, data, content_type):
        """Serialize a dictionary into the specified content type."""
        return self._get_serialize_handler(content_type).serialize(data)

    def deserialize(self, datastring, content_type):
        """Deserialize a string to a dictionary.
        The string must be in the format of a supported MIME type.
        """
        try:
            return self.get_deserialize_handler(content_type).deserialize(
                datastring)
        except Exception:
            raise webob.exc.HTTPBadRequest(_("Could not deserialize data"))

    def get_deserialize_handler(self, content_type):
        handlers = {
            'application/json': JSONDeserializer(),
        }

        try:
            return handlers[content_type]
        except Exception:
            raise exceptions.InvalidContentType(content_type=content_type)