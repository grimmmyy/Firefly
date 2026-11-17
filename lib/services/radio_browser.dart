import 'dart:convert';

import 'package:http/http.dart' as http;

/// A station from the free, community-run Radio Browser directory.
class RadioStation {
  final String name;
  final String url;
  final String favicon;
  final String tags;
  final String country;
  final String codec;
  final int bitrate;

  RadioStation({
    required this.name,
    required this.url,
    this.favicon = '',
    this.tags = '',
    this.country = '',
    this.codec = '',
    this.bitrate = 0,
  });

  /// MIME type for the receiver; the RX-V671 plays MP3 and AAC streams.
  String get mime => codec.toUpperCase().contains('AAC') ? 'audio/aac' : 'audio/mpeg';

  factory RadioStation.fromJson(Map<String, dynamic> j) => RadioStation(
        name: (j['name'] as String? ?? '').trim(),
        url: (j['url_resolved'] as String?)?.isNotEmpty == true
            ? j['url_resolved'] as String
            : j['url'] as String? ?? '',
        favicon: j['favicon'] as String? ?? '',
        tags: j['tags'] as String? ?? '',
        country: j['countrycode'] as String? ?? '',
        codec: j['codec'] as String? ?? '',
        bitrate: (j['bitrate'] as num?)?.toInt() ?? 0,
      );
}

class RadioBrowser {
  // Round-robin mirrors; fall through if one is down.
  static const _mirrors = [
    'de1.api.radio-browser.info',
    'nl1.api.radio-browser.info',
    'at1.api.radio-browser.info',
  ];

  Future<List<RadioStation>> _get(String path, Map<String, String> q) async {
    for (final m in _mirrors) {
      try {
        final r = await http.get(Uri.https(m, path, q),
            headers: {'User-Agent': 'Firefly/1.0'}).timeout(const Duration(seconds: 8));
        if (r.statusCode != 200) continue;
        final list = (jsonDecode(r.body) as List).cast<Map<String, dynamic>>();
        return list
            .map(RadioStation.fromJson)
            // the receiver only handles MP3/AAC streams
            .where((s) =>
                s.url.isNotEmpty &&
                (s.codec.toUpperCase().contains('MP3') ||
                    s.codec.toUpperCase().contains('AAC')))
            .toList();
      } catch (_) {}
    }
    return [];
  }

  Future<List<RadioStation>> search(String query) => _get('/json/stations/search', {
        'name': query,
        'limit': '40',
        'hidebroken': 'true',
        'order': 'clickcount',
        'reverse': 'true',
      });

  Future<List<RadioStation>> top({String country = 'US'}) =>
      _get('/json/stations/search', {
        'countrycode': country,
        'limit': '40',
        'hidebroken': 'true',
        'order': 'clickcount',
        'reverse': 'true',
      });

  Future<List<RadioStation>> byTag(String tag) => _get('/json/stations/search', {
        'tag': tag,
        'limit': '40',
        'hidebroken': 'true',
        'order': 'clickcount',
        'reverse': 'true',
      });
}
