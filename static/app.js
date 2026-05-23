// Alpine.js global store for shared state
document.addEventListener('alpine:init', () => {
  Alpine.store('app', {
    syncing: false,
  });
});
