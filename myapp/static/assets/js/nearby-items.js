/**
 * Nearby Widget: a one-shot location check on Inventory load - if the
 * device's current coordinates are within range of a shop matching one of
 * the user's item issuers (per OpenStreetMap, matched server-side), shows
 * a dismissible-by-navigation-away "Nearby" card linking straight to the
 * item. Never watches position continuously and never repeats the
 * request itself; the coordinates never leave this one request to the
 * server (see myapp/nearby_places.py) and are never stored.
 */
function vvInitNearbyItems(config) {
  config = config || {};
  var widget = document.getElementById('nearby-items-widget');
  var list = document.getElementById('nearby-items-list');
  if (!widget || !list || !navigator.geolocation) return;

  function renderItems(items) {
    if (!items || !items.length) return;
    list.innerHTML = '';
    items.forEach(function (item) {
      var li = document.createElement('li');
      var a = document.createElement('a');
      a.className = 'nearby-item-link';
      a.href = item.url;

      var text = document.createElement('div');
      text.className = 'nearby-item-text';

      var issuer = document.createElement('div');
      issuer.className = 'nearby-item-issuer';
      issuer.textContent = item.issuer;

      var name = document.createElement('div');
      name.className = 'nearby-item-name';
      name.textContent = item.name;

      text.appendChild(issuer);
      text.appendChild(name);
      a.appendChild(text);
      a.insertAdjacentHTML('beforeend', '<i class="bi bi-chevron-right"></i>');
      li.appendChild(a);
      list.appendChild(li);
    });
    widget.hidden = false;
  }

  function fetchNearby(lat, lon) {
    var csrfInput = document.querySelector('[name=csrfmiddlewaretoken]');
    var body = new URLSearchParams();
    body.set('lat', lat);
    body.set('lon', lon);
    fetch(config.nearbyUrl, {
      method: 'POST',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': csrfInput ? csrfInput.value : '',
      },
      body: body,
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (data && data.items) renderItems(data.items);
      })
      .catch(function () {});
  }

  navigator.geolocation.getCurrentPosition(
    function (position) {
      fetchNearby(position.coords.latitude, position.coords.longitude);
    },
    function () { /* denied, unavailable, or timed out - fail silently, no nag */ },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 }
  );
}
