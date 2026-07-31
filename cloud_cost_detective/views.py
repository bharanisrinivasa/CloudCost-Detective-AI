from django.db import connections
from django.db.utils import OperationalError
from django.http import JsonResponse

def health_check(request):
    """
    Lightweight application liveness endpoint.
    Must not perform expensive operations or external API calls.
    """
    return JsonResponse({"status": "ok"})

def readiness_check(request):
    """
    Readiness endpoint verifying database connectivity.
    """
    try:
        db_conn = connections["default"]
        with db_conn.cursor() as cursor:
            cursor.execute("SELECT 1")
    except OperationalError:
        return JsonResponse({"status": "unavailable"}, status=503)
    except Exception:
        # Fails closed safely on other exceptions
        return JsonResponse({"status": "unavailable"}, status=503)
    
    return JsonResponse({"status": "ready"})
