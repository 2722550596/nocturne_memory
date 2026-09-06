import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
// jsdom under vitest does not always expose a global `localStorage`; api.js
// reads it bare (not via window), so stub a minimal in-memory version.
const _store = new Map();
globalThis.localStorage = {
  getItem: (k) => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => _store.set(k, String(v)),
  removeItem: (k) => _store.delete(k),
  clear: () => _store.clear(),
};

// The backend /api/browse/search returns a BARE array (list of hits), NOT an
// object with a `results` field. The regression we lock down is that the UI
// renders that array instead of silently reading `res.results` (which is
// always undefined on a list).
const hoisted = vi.hoisted(() => {
  const searchMemories = vi.fn();
  // A stubbed axios instance so component-init GETs (e.g. /browse/node) don't
  // hit the network. /browse/node returns an empty root node; everything else
  // resolves to {}.
  const stubApi = {
    get: vi.fn(async (url) => {
      if (url.includes('/browse/node')) {
        return { data: { node: null, children: [], breadcrumbs: [] } };
      }
      return { data: {} };
    }),
    put: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async () => ({ data: {} })),
    delete: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  };
  return { searchMemories, stubApi };
});

vi.mock('../../../lib/api', async () => {
  return {
    api: hoisted.stubApi,
    searchMemories: hoisted.searchMemories,
    getDomains: vi.fn().mockResolvedValue([]),
    getNamespaces: vi.fn().mockResolvedValue([]),
    getSettingsBootUris: vi.fn().mockResolvedValue({ uris: [] }),
    createMemory: vi.fn(),
    renameNode: vi.fn(),
    addDomain: vi.fn(),
    removeDomain: vi.fn(),
    deleteNode: vi.fn(),
    toggleSettingsBootUri: vi.fn(),
  };
});

// Fully stub react-router so useSearchParams works without a Router context.
vi.mock('react-router-dom', () => ({
  useSearchParams: () => [new URLSearchParams('domain=core'), vi.fn()],
  useNavigate: () => vi.fn(),
  MemoryRouter: ({ children }) => children,
  BrowserRouter: ({ children }) => children,
}));

import MemoryBrowser from '../MemoryBrowser.jsx';

beforeEach(() => {
  vi.clearAllMocks();
  hoisted.searchMemories.mockReset();
});

describe('MemoryBrowser search contract', () => {
  it('renders results returned as a bare array from the backend', async () => {
    hoisted.searchMemories.mockResolvedValue([
      {
        uri: 'core://magic_system',
        name: 'magic_system',
        snippet: '这是关于魔法系统的设定笔记',
        priority: 2,
        domain: 'core',
        path: 'magic_system',
      },
    ]);

    render(<MemoryBrowser />);

    const input = screen.getByPlaceholderText(/search/i);
    fireEvent.change(input, { target: { value: '魔法' } });

    await waitFor(
      () => expect(screen.getByText('core://magic_system')).toBeInTheDocument(),
      { timeout: 2000 }
    );
  });

  it('does not crash and shows no results on an empty array', async () => {
    hoisted.searchMemories.mockResolvedValue([]);

    render(<MemoryBrowser />);

    const input = screen.getByPlaceholderText(/search/i);
    fireEvent.change(input, { target: { value: '不存在的东西' } });

    await waitFor(
      () => expect(hoisted.searchMemories).toHaveBeenCalled(),
      { timeout: 2000 }
    );
    expect(screen.queryByText('core://magic_system')).not.toBeInTheDocument();
  });
});
