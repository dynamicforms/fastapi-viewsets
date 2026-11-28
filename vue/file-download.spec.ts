/** Unit tests for file-download.ts: the file name in Content-Disposition and the FileDownload of a response. */

import { AxiosHeaders } from 'axios';
import { describe, expect, it } from 'vitest';

import { contentDispositionFilename, fileDownload, isJsonContentType } from './file-download';

describe('contentDispositionFilename', () => {
  it.each([
    ['attachment; filename="track-1.pdf"; filename*=UTF-8\'\'track-1-%C3%A9.pdf', 'track-1-é.pdf'],
    ['attachment; filename*=UTF-8\'\'track-1-%C3%A9.pdf; filename="track-1.pdf"', 'track-1-é.pdf'],
    ["attachment; FILENAME*=utf-8'en'%E2%82%AC%20rates.txt", '€ rates.txt'],
    ["attachment; filename*=iso-8859-1'en'%A3%20rates.txt", '£ rates.txt'],
    ['attachment; filename="a \\"quoted\\" name.pdf"', 'a "quoted" name.pdf'],
    ['attachment; filename="semi;colon.pdf"', 'semi;colon.pdf'],
    ['attachment; filename=export.csv', 'export.csv'],
    ['inline;filename=report.pdf', 'report.pdf'],
  ])('reads %s', (header, filename) => {
    expect(contentDispositionFilename(header)).toBe(filename);
  });

  it('falls back to filename when filename* does not decode', () => {
    expect(contentDispositionFilename("attachment; filename*=UTF-8''%E9.pdf; filename=plain.pdf")).toBe('plain.pdf');
    expect(contentDispositionFilename("attachment; filename*=koi8-r''x.pdf; filename=plain.pdf")).toBe('plain.pdf');
  });

  it.each([null, undefined, '', 'attachment', 'attachment; filename=""', 'inline; size=3'])(
    'is null for %s',
    (header) => {
      expect(contentDispositionFilename(header)).toBeNull();
    },
  );
});

describe('fileDownload', () => {
  it('keeps a Blob of the response type and lower-cases the header names', () => {
    const blob = new Blob(['%PDF'], { type: 'application/pdf' });
    const download = fileDownload(
      blob,
      new AxiosHeaders({ 'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename=a.pdf' }),
    );
    expect(download).toEqual({
      blob,
      filename: 'a.pdf',
      contentType: 'application/pdf',
      headers: { 'content-type': 'application/pdf', 'content-disposition': 'attachment; filename=a.pdf' },
    });
  });

  it('wraps another body in a Blob of the Content-Type', () => {
    const download = fileDownload('a,b\n', { 'content-type': 'text/csv' });
    expect(download.blob).toBeInstanceOf(Blob);
    expect(download.blob.type).toBe('text/csv');
    expect(download.blob.size).toBe(4);
  });

  it('has an empty contentType and no file name without those headers', () => {
    const download = fileDownload(new Blob(['x']), undefined);
    expect(download.contentType).toBe('');
    expect(download.filename).toBeNull();
    expect(download.headers).toEqual({});
  });
});

describe('isJsonContentType', () => {
  it.each([
    ['application/json', true],
    ['application/json; charset=utf-8', true],
    ['Application/Problem+JSON', true],
    ['application/pdf', false],
    ['text/html', false],
    ['', false],
    [undefined, false],
  ])('%s → %s', (contentType, expected) => {
    expect(isJsonContentType(contentType)).toBe(expected);
  });
});
