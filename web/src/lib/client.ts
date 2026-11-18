import createClient from 'openapi-fetch';
import type { paths } from './api';

/** Typed client for the `lure serve` API; types come from `npm run gen:api`. */
export const api = createClient<paths>({ baseUrl: '/' });
