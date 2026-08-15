document.addEventListener('DOMContentLoaded', () => {
  const body = document.body;
  const header = document.querySelector('.site-header');
  const menuButton = document.querySelector('.menu-toggle');
  const navigation = document.querySelector('.site-nav');
  const searchButton = document.querySelector('.search-toggle');
  const searchDrawer = document.querySelector('.search-drawer');
  const searchClose = document.querySelector('.search-close');
  const searchInput = document.querySelector('.header-search input');

  const closeMenu = () => {
    if (!menuButton || !navigation) return;
    menuButton.setAttribute('aria-expanded', 'false');
    navigation.classList.remove('is-open');
  };

  const closeSearch = () => {
    if (!searchButton || !searchDrawer) return;
    searchButton.setAttribute('aria-expanded', 'false');
    searchDrawer.setAttribute('aria-hidden', 'true');
    searchDrawer.inert = true;
    searchDrawer.classList.remove('is-open');
    body.classList.remove('search-open');
  };

  const openSearch = () => {
    if (!searchButton || !searchDrawer) return;
    closeMenu();
    searchButton.setAttribute('aria-expanded', 'true');
    searchDrawer.setAttribute('aria-hidden', 'false');
    searchDrawer.inert = false;
    searchDrawer.classList.add('is-open');
    body.classList.add('search-open');
    window.setTimeout(() => searchInput?.focus(), 120);
  };

  if (menuButton && navigation) {
    menuButton.addEventListener('click', () => {
      const willOpen = menuButton.getAttribute('aria-expanded') !== 'true';
      closeSearch();
      menuButton.setAttribute('aria-expanded', String(willOpen));
      navigation.classList.toggle('is-open', willOpen);
    });

    navigation.addEventListener('click', (event) => {
      if (event.target.closest('a')) closeMenu();
    });
  }

  searchButton?.addEventListener('click', () => {
    if (searchButton.getAttribute('aria-expanded') === 'true') closeSearch();
    else openSearch();
  });
  searchClose?.addEventListener('click', closeSearch);

  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) {
      event.preventDefault();
      openSearch();
    }
    if (event.key === 'Escape') {
      closeSearch();
      closeMenu();
      searchButton?.focus();
    }
  });

  document.addEventListener('click', (event) => {
    if (searchDrawer?.classList.contains('is-open') && !header?.contains(event.target)) closeSearch();
  });

  const updateHeader = () => header?.classList.toggle('is-scrolled', window.scrollY > 18);
  updateHeader();
  window.addEventListener('scroll', updateHeader, { passive: true });

  document.querySelectorAll('.poster-frame img, .detail-poster img').forEach((image) => {
    image.addEventListener('error', () => image.closest('.poster-frame, .detail-poster')?.classList.add('image-failed'));
  });

  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const revealItems = [...document.querySelectorAll('[data-reveal]')];
    if (revealItems.length && 'IntersectionObserver' in window) {
      body.classList.add('reveal-ready');
      const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add('is-revealed');
          observer.unobserve(entry.target);
        });
      }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
      revealItems.forEach((item) => observer.observe(item));
    }
  }

  const primaryPreferences = document.getElementById('open_preferences_center');
  const inlinePreferences = document.getElementById('open_preferences_center_inline');
  if (primaryPreferences && inlinePreferences) {
    inlinePreferences.addEventListener('click', () => primaryPreferences.click());
  }
});
