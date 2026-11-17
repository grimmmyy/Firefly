import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:xml/xml.dart';

import 'ssdp.dart';

/// UPnP AVTransport + RenderingControl SOAP client.
/// Just enough to drive a DLNA MediaRenderer like the Yamaha RX-V671.
class UpnpClient {
  final Renderer renderer;
  UpnpClient(this.renderer);

  static const _avt = 'urn:schemas-upnp-org:service:AVTransport:1';
  static const _rc = 'urn:schemas-upnp-org:service:RenderingControl:1';

  Future<void> setAvTransportUri(
    String mediaUrl, {
    String? title,
    String? mime,
  }) async {
    final metadata = _didlLite(mediaUrl, title: title, mime: mime);
    await _soap(
      renderer.avTransportControlUrl,
      _avt,
      'SetAVTransportURI',
      {
        'InstanceID': '0',
        'CurrentURI': mediaUrl,
        'CurrentURIMetaData': metadata,
      },
    );
  }

  Future<void> play() => _soap(
        renderer.avTransportControlUrl,
        _avt,
        'Play',
        {'InstanceID': '0', 'Speed': '1'},
      );

  Future<void> pause() => _soap(
        renderer.avTransportControlUrl,
        _avt,
        'Pause',
        {'InstanceID': '0'},
      );

  Future<void> stop() => _soap(
        renderer.avTransportControlUrl,
        _avt,
        'Stop',
        {'InstanceID': '0'},
      );

  Future<void> setVolume(int volume) => _soap(
        renderer.renderingControlUrl,
        _rc,
        'SetVolume',
        {
          'InstanceID': '0',
          'Channel': 'Master',
          'DesiredVolume': '${volume.clamp(0, 100)}',
        },
      );

  Future<int> getVolume() async {
    final body = await _soap(
      renderer.renderingControlUrl,
      _rc,
      'GetVolume',
      {'InstanceID': '0', 'Channel': 'Master'},
    );
    final doc = XmlDocument.parse(body);
    final v = doc.findAllElements('CurrentVolume').firstOrNull?.innerText;
    return int.tryParse(v ?? '') ?? 0;
  }

  Future<Map<String, String>> getTransportInfo() async {
    final body = await _soap(
      renderer.avTransportControlUrl,
      _avt,
      'GetTransportInfo',
      {'InstanceID': '0'},
    );
    final doc = XmlDocument.parse(body);
    String val(String n) =>
        doc.findAllElements(n).firstOrNull?.innerText ?? '';
    return {
      'state': val('CurrentTransportState'),
      'status': val('CurrentTransportStatus'),
      'speed': val('CurrentSpeed'),
    };
  }

  Future<String> _soap(
    Uri controlUrl,
    String serviceType,
    String action,
    Map<String, String> args,
  ) async {
    final argsXml = args.entries
        .map((e) => '<${e.key}>${_xmlEscape(e.value)}</${e.key}>')
        .join();
    final envelope =
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<u:$action xmlns:u="$serviceType">$argsXml</u:$action>'
        '</s:Body></s:Envelope>';

    final resp = await http.post(
      controlUrl,
      headers: {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': '"$serviceType#$action"',
      },
      body: utf8.encode(envelope),
    );
    if (resp.statusCode >= 400) {
      throw Exception(
          'SOAP $action failed ${resp.statusCode}: ${resp.body}');
    }
    return resp.body;
  }

  String _didlLite(String url, {String? title, String? mime}) {
    final t = title ?? Uri.parse(url).pathSegments.lastWhere(
          (s) => s.isNotEmpty,
          orElse: () => 'Audio',
        );
    final protocolInfo = 'http-get:*:${mime ?? 'audio/mpeg'}:*';
    final didl =
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        '<dc:title>${_xmlEscape(t)}</dc:title>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        '<res protocolInfo="$protocolInfo">${_xmlEscape(url)}</res>'
        '</item></DIDL-Lite>';
    return didl;
  }

  String _xmlEscape(String s) => s
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
}
