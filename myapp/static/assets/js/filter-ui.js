(function () {
  var MOBILE_BREAKPOINT = 768;

  // Auto-focus a search input on desktop (or if it already has a value, to
  // preserve cursor position after a search navigation) - skipped on
  // mobile with an empty value so the on-screen keyboard doesn't pop up
  // unprompted. Shared by inventory.html and sharing_center.html, which
  // previously carried identical copies of this logic.
  window.autoFocusSearchOnDesktop = function (searchInput) {
    if (!searchInput) return;
    var isDesktop = window.innerWidth > MOBILE_BREAKPOINT;
    if (isDesktop || searchInput.value) {
      searchInput.focus();
      searchInput.setSelectionRange(searchInput.value.length, searchInput.value.length);
    }
  };

  // Reads one or more mobile filter <select> elements plus (optionally)
  // the search input, and navigates to the resulting filtered URL -
  // the "sync mobile dropdown(s) -> reload page with matching query
  // params" pattern shared by inventory.html (status + type selects) and
  // sharing_center.html (a single filter select), previously implemented
  // twice with inconsistent URL-building (URLSearchParams vs raw string
  // concatenation).
  //
  // paramMap: { queryParamName: selectElementId, ... }
  // options.searchInputId: id of the search input to preserve (default 'searchInput')
  // options.preserveQuery: set false to skip carrying over the search query
  window.applyMobileDropdownFilters = function (paramMap, options) {
    options = options || {};
    var url = new URL(window.location.href);

    Object.keys(paramMap).forEach(function (param) {
      var el = document.getElementById(paramMap[param]);
      if (!el) return;
      if (el.value) {
        url.searchParams.set(param, el.value);
      } else {
        url.searchParams.delete(param);
      }
    });

    if (options.preserveQuery !== false) {
      var searchInput = document.getElementById(options.searchInputId || 'searchInput');
      if (searchInput && searchInput.value.trim()) {
        url.searchParams.set('query', searchInput.value.trim());
      }
    }

    window.location.href = url.toString();
  };
})();
