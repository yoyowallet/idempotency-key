import logging
import threading

from django.core.exceptions import ImproperlyConfigured

from idempotency_key.exceptions import DecoratorsMutuallyExclusiveError, bad_request, resource_locked
from idempotency_key.utils import get_storage_class, get_encoder_class, get_conflict_code, get_lock_timeout, \
    get_enable_lock, get_store_on_statuses

logger = logging.getLogger('django-idempotency-key.idempotency_key.middleware')


class IdempotencyKeyMiddleware:
    """
    This middleware class assumes that all non-safe HTTP methods will require an idempotency key to be specified in
    the header.
    View functions can opt-out using the @idempotency_key_exempt decorator
    """

    def __init__(self, get_response=None):
        self.get_response = get_response
        self.storage = get_storage_class()()
        self.encoder = get_encoder_class()()
        self.storage_lock = threading.Lock()

    def __call__(self, request):
        self.process_request(request)
        response = self.get_response(request)
        response = self.process_response(request, response)
        return response

    @staticmethod
    def _reject(request, reason):
        response = bad_request(request, None)
        logger.debug(
            'Bad Request (%s): %s', reason, request.path,
            extra={
                'status_code': 400,
                'request': request,
            }
        )
        return response

    def _set_flags_from_callback(self, request, callback):
        # If there is an actions attribute then the function is wrapped in a DRF viewset
        func_name = callback.__name__
        if hasattr(callback, 'actions'):
            func_name = callback.actions[request.method.lower()]
            # get a reference to the function to access any attributes we might be interested in.
            callback = getattr(callback.cls, func_name, callback)

        idempotency_key = getattr(callback, 'idempotency_key', None)
        idempotency_key_exempt = getattr(callback, 'idempotency_key_exempt', False)
        idempotency_key_manual = getattr(callback, 'idempotency_key_manual', False)

        if idempotency_key and idempotency_key_exempt:
            raise DecoratorsMutuallyExclusiveError(
                '@idempotency_key and @idempotency_key_exempt decorators are mutually exclusive for '
                'function "{}"'.format(func_name))

        if idempotency_key_manual and idempotency_key_exempt:
            raise DecoratorsMutuallyExclusiveError(
                '@idempotency_key_manual and @idempotency_key_exempt decorators are mutually exclusive for '
                'function "{}"'.format(func_name))

        request.idempotency_key_exempt = idempotency_key_exempt
        request.idempotency_key_manual = idempotency_key_manual

    def perform_generate_response(self, request, encoded_key):
        # Check if a response already exists for the encoded key
        key_exists, response = self.storage.retrieve_data(encoded_key)

        # add the key exists result and the original request if it exists
        request.idempotency_key_exists = key_exists
        request.idempotency_key_response = response

        # If not manual override and the key already exists
        if not request.idempotency_key_manual and key_exists:
            # Get the required return status code from settings
            status_code = get_conflict_code()
            # if None then return whatever the status code was originally otherwise use the specified status code
            if status_code is not None:
                response.status_code = status_code
            return response

        return None

    def generate_response(self, request, encoded_key, lock=None):
        if lock is None:
            lock = get_enable_lock()

        if not lock:
            return self.perform_generate_response(request, encoded_key)

        lock_result = self.storage_lock.acquire(blocking=True, timeout=get_lock_timeout())
        # If there was a timeout for a lock on the storage object then return a HTTP_423_LOCKED
        if not lock_result:
            return resource_locked(request, None)

        try:
            return self.perform_generate_response(request, encoded_key)
        finally:
            self.storage_lock.release()

    def process_request(self, request):
        key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if key is not None:
            request.META['IDEMPOTENCY_KEY'] = key

        # Use this attribute to check that process_view has been called.
        request.idempotency_key_done = False

    def process_view(self, request, callback, callback_args, callback_kwargs):
        self._set_flags_from_callback(request, callback)

        # signal the process_view has been called
        request.idempotency_key_done = True

        # Assume that anything defined as 'safe' by RFC7231 is exempt or if exempt is specified directly
        if request.idempotency_key_exempt or request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            request.idempotency_key_exempt = True
            return None

        # At this point the view function is not exempt so mark it as such
        request.idempotency_key_exempt = False

        key = request.META.get('IDEMPOTENCY_KEY')
        if key is None:
            return self._reject(request, 'Idempotency key is required and was not specified in the header.')

        # encode the key and add it to the request
        encoded_key = request.idempotency_key_encoded_key = self.encoder.encode_key(request, key)

        # Generate the response
        return self.generate_response(request, encoded_key)

    def process_response(self, request, response):
        # if there has been a server error then just return the response
        if response and response.status_code in [500]:
            return response

        # Make sure that process_view is called otherwise the use of idempotency keys will be overridden without us
        # knowing about it.
        if not getattr(request, 'idempotency_key_done', False):
            raise ImproperlyConfigured(
                'Idempotency key middleware\'s \'process_view\' function was not called! '
                'There maybe another middleware stopping this from happening which means your functions will not '
                'be properly protected with idempotency keys.'
            )

        if getattr(request, 'idempotency_key_exempt', True):
            return response

        if request.method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            # If the response matches that given by the store_on_statuses function then store the data
            if response.status_code in get_store_on_statuses():
                self.storage.store_data(request.idempotency_key_encoded_key, response)

        return response


class ExemptIdempotencyKeyMiddleware(IdempotencyKeyMiddleware):
    """
    This middleware class assume all requests are exempt and do not require an idempotency key to be specified.
    View functions opt-in using the @idempotency_key or @idempotency_key_manual decorators.
    """

    def _set_flags_from_callback(self, request, callback):
        func_name = callback.__name__
        # If there is an actions attribute then the function is wrapped in a DRF viewset
        if hasattr(callback, 'actions'):
            func_name = callback.actions[request.method.lower()]
            # get a reference to the function to access any attributes we might be interested in.
            callback = getattr(callback.cls, func_name, callback)

        idempotency_key = getattr(callback, 'idempotency_key', False)
        idempotency_key_exempt = getattr(callback, 'idempotency_key_exempt', None)
        idempotency_key_manual = getattr(callback, 'idempotency_key_manual', False)

        if idempotency_key and idempotency_key_exempt:
            raise DecoratorsMutuallyExclusiveError(
                '@idempotency_key and @idempotency_key_exempt decorators are mutually exclusive for '
                'function "{}"'.format(func_name))

        if idempotency_key_manual and idempotency_key_exempt:
            raise DecoratorsMutuallyExclusiveError(
                '@idempotency_key_manual and @idempotency_key_exempt decorators are mutually exclusive for '
                'function "{}"'.format(func_name))

        request.idempotency_key_exempt = idempotency_key_exempt or (
                idempotency_key_exempt is None and not idempotency_key_manual and not idempotency_key
        )

        request.idempotency_key_manual = idempotency_key_manual
