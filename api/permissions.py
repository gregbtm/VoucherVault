from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    """
    Object-level check that the requesting user owns the object.
    Defense-in-depth alongside get_queryset() user filtering — every
    queryset in this app is already scoped to request.user, so this
    should never actually reject anything reachable via the API.
    """

    def has_object_permission(self, request, view, obj):
        return getattr(obj, 'user_id', None) == request.user.id
