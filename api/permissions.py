from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsOwner(BasePermission):
    """
    Object-level check that the requesting user owns the object.
    Defense-in-depth alongside get_queryset() user filtering — every
    queryset in this app is already scoped to request.user, so this
    should never actually reject anything reachable via the API.
    """

    def has_object_permission(self, request, view, obj):
        return getattr(obj, 'user_id', None) == request.user.id


class IsItemOwnerOrWalletCollaborator(BasePermission):
    """
    Full read/write for the item's owner, and for any user the item's
    wallet (if any) has been shared with.
    """

    def has_object_permission(self, request, view, obj):
        if obj.user_id == request.user.id:
            return True
        return obj.wallet_id is not None and obj.wallet.shared_with.filter(pk=request.user.id).exists()


class IsWalletOwnerOrReadOnlyCollaborator(BasePermission):
    """
    Full read/write for the wallet's owner. Collaborators the wallet has
    been shared with get read-only access to the wallet object itself
    (they still get full read/write on the items inside it, via
    IsItemOwnerOrWalletCollaborator).
    """

    def has_object_permission(self, request, view, obj):
        if obj.user_id == request.user.id:
            return True
        if request.method in SAFE_METHODS:
            return obj.shared_with.filter(pk=request.user.id).exists()
        return False
