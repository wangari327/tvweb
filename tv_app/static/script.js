document.addEventListener('DOMContentLoaded', () => {
  const menuButton = document.querySelector('.menu-toggle');
  const navigation = document.querySelector('.site-nav');

  if (menuButton && navigation) {
    menuButton.addEventListener('click', () => {
      const isOpen = menuButton.getAttribute('aria-expanded') === 'true';
      menuButton.setAttribute('aria-expanded', String(!isOpen));
      navigation.classList.toggle('is-open', !isOpen);
    });

    navigation.addEventListener('click', (event) => {
      if (event.target.closest('a')) {
        menuButton.setAttribute('aria-expanded', 'false');
        navigation.classList.remove('is-open');
      }
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) {
      const search = document.querySelector('.header-search input');
      if (search) {
        event.preventDefault();
        search.focus();
      }
    }
  });

  document.querySelectorAll('.poster-frame img, .detail-poster img').forEach((image) => {
    image.addEventListener('error', () => image.closest('.poster-frame, .detail-poster')?.classList.add('image-failed'));
  });

  const primaryPreferences = document.getElementById('open_preferences_center');
  const inlinePreferences = document.getElementById('open_preferences_center_inline');
  if (primaryPreferences && inlinePreferences) {
    inlinePreferences.addEventListener('click', () => primaryPreferences.click());
  }
});
