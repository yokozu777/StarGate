/**
 * Server Logs: Utility Functions
 * Phase 4: Hooks/Utilities for common operations
 */

// Phase 4: Debounce utility (vanilla JS version of React hook)
function useDebounce(value, delay) {
  let timeoutId;
  return function debounced(...args) {
    clearTimeout(timeoutId);
    timeoutId = setTimeout(() => {
      if (typeof value === 'function') {
        value(...args);
      }
    }, delay);
  };
}

// Phase 4: Memoization utility
function useMemo(fn, deps) {
  const cache = new Map();
  const key = JSON.stringify(deps);
  
  if (cache.has(key)) {
    return cache.get(key);
  }
  
  const result = fn();
  cache.set(key, result);
  return result;
}

// Phase 4: Throttle utility
function useThrottle(fn, delay) {
  let lastCall = 0;
  return function throttled(...args) {
    const now = Date.now();
    if (now - lastCall >= delay) {
      lastCall = now;
      return fn(...args);
    }
  };
}

// Export for use in main app
if (typeof window !== 'undefined') {
  window.ServerLogsUtils = {
    useDebounce,
    useMemo,
    useThrottle
  };
}
