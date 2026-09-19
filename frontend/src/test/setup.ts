import "@testing-library/jest-dom";

Object.defineProperty(navigator, "serviceWorker", {
  value: {
    ready: Promise.resolve({
      pushManager: {
        getSubscription: () => Promise.resolve(null),
      },
    }),
  },
  configurable: true,
});
