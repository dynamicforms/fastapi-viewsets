import { AxiosError, AxiosHeaders } from 'axios';

import { ERROR_NAMESPACE, ERROR_NAMESPACE_PYDANTIC } from './error-namespace';
import {
  type ApiErrorBody,
  errorBodyOf,
  isApiErrorBody,
  toErrorDescription,
  toFieldErrors,
  ViewSetRequestError,
} from './errors';

import * as entry from './index';

const NOT_FOUND: ApiErrorBody = {
  detail: 'Item with pk 5 not found',
  detail_code: 'fastapi-viewsets.not_found',
  detail_params: { pk: 5 },
  field_errors: {},
};

const VALIDATION_FAILED: ApiErrorBody = {
  detail: 'The request failed validation',
  detail_code: 'fastapi-viewsets.validation_failed',
  detail_params: {},
  field_errors: {
    name: [
      {
        code: 'pydantic.string_too_short',
        params: { min_length: 3 },
        detail: 'String should have at least 3 characters',
      },
    ],
    '': [{ code: 'pydantic.missing', params: {}, detail: 'Field required' }],
    'items.0': [
      { code: 'pydantic.a', params: {}, detail: 'first' },
      { code: 'pydantic.b', params: {}, detail: 'second' },
    ],
  },
};

function axiosError(status: number, data: unknown): AxiosError {
  const config = { headers: new AxiosHeaders() };
  return new AxiosError('failed', 'ERR_BAD_RESPONSE', config, null, {
    status,
    statusText: '',
    headers: {},
    config,
    data,
  });
}

describe('isApiErrorBody', () => {
  it('should accept the uniform body only', () => {
    expect(isApiErrorBody(NOT_FOUND)).toBe(true);
    expect(isApiErrorBody(VALIDATION_FAILED)).toBe(true);
    expect(isApiErrorBody({ detail: 'Not Found' })).toBe(false);
    expect(isApiErrorBody({ detail: [{ type: 'missing', loc: ['body'], msg: 'Field required' }] })).toBe(false);
    expect(isApiErrorBody({ ...NOT_FOUND, detail_params: null })).toBe(false);
    expect(isApiErrorBody({ ...NOT_FOUND, field_errors: [] })).toBe(false);
    expect(isApiErrorBody('<html>Bad Gateway</html>')).toBe(false);
    expect(isApiErrorBody(null)).toBe(false);
  });
});

describe('toErrorDescription', () => {
  it('should take the code, params and detail of the body, with origin server', () => {
    expect(toErrorDescription(NOT_FOUND)).toEqual({
      code: 'fastapi-viewsets.not_found',
      params: { pk: 5 },
      detail: 'Item with pk 5 not found',
      origin: 'server',
    });
  });

  it('should pass an application code on as the server sent it', () => {
    expect(toErrorDescription({ ...NOT_FOUND, detail_code: 'app.conflict' }).code).toBe('app.conflict');
    expect(toErrorDescription({ ...NOT_FOUND, detail_code: 'conflict' }).code).toBe('conflict');
  });

  it('should describe an http_error with its status', () => {
    const error = toErrorDescription({
      detail: 'Not your item',
      detail_code: 'fastapi-viewsets.http_error',
      detail_params: { status: 403 },
      field_errors: {},
    });
    expect(error).toEqual({
      code: 'fastapi-viewsets.http_error',
      params: { status: 403 },
      detail: 'Not your item',
      origin: 'server',
    });
  });
});

describe('toFieldErrors', () => {
  it('should give each field its errors as the server keyed them, with origin server', () => {
    expect(toFieldErrors(VALIDATION_FAILED)).toEqual({
      name: [
        {
          code: 'pydantic.string_too_short',
          params: { min_length: 3 },
          detail: 'String should have at least 3 characters',
          origin: 'server',
        },
      ],
      '': [{ code: 'pydantic.missing', params: {}, detail: 'Field required', origin: 'server' }],
      'items.0': [
        { code: 'pydantic.a', params: {}, detail: 'first', origin: 'server' },
        { code: 'pydantic.b', params: {}, detail: 'second', origin: 'server' },
      ],
    });
  });

  it('should give a body without field errors none', () => {
    expect(toFieldErrors(NOT_FOUND)).toEqual({});
  });
});

describe('errorBodyOf', () => {
  it('should return the body of an answer over axios and over muxws as the server sent it', () => {
    expect(errorBodyOf(axiosError(404, NOT_FOUND))).toBe(NOT_FOUND);
    expect(errorBodyOf(new ViewSetRequestError(422, VALIDATION_FAILED))).toBe(VALIDATION_FAILED);
  });

  it('should describe an error without a response as no_response', () => {
    const expected: ApiErrorBody = {
      detail: 'The server did not answer',
      detail_code: `${ERROR_NAMESPACE}.no_response`,
      detail_params: {},
      field_errors: {},
    };
    expect(errorBodyOf(new AxiosError('Network Error', 'ERR_NETWORK'))).toEqual(expected);
    expect(errorBodyOf(new Error('stream reset'))).toEqual(expected);
    expect(errorBodyOf(undefined)).toEqual(expected);
  });

  it('should describe a response whose body is not an error body as unexpected_response with its status', () => {
    expect(errorBodyOf(axiosError(502, '<html>Bad Gateway</html>'))).toEqual({
      detail: 'The server answered with status 502 and a body that is not an error body',
      detail_code: `${ERROR_NAMESPACE}.unexpected_response`,
      detail_params: { status: 502 },
      field_errors: {},
    });
    expect(errorBodyOf(axiosError(404, { detail: 'Not Found' })).detail_params).toEqual({ status: 404 });
    expect(errorBodyOf({ response: {} }).detail_params).toEqual({ status: null });
  });
});

describe('error namespaces', () => {
  it('should be exported from the package root with the values of the Python package', () => {
    expect(entry.ERROR_NAMESPACE).toBe('fastapi-viewsets');
    expect(entry.ERROR_NAMESPACE_PYDANTIC).toBe('pydantic');
    expect([ERROR_NAMESPACE, ERROR_NAMESPACE_PYDANTIC]).toEqual([
      entry.ERROR_NAMESPACE,
      entry.ERROR_NAMESPACE_PYDANTIC,
    ]);
  });

  it('should export the error helpers from the package root', () => {
    expect(entry.errorBodyOf).toBe(errorBodyOf);
    expect(entry.isApiErrorBody).toBe(isApiErrorBody);
    expect(entry.toErrorDescription).toBe(toErrorDescription);
    expect(entry.toFieldErrors).toBe(toFieldErrors);
  });
});
