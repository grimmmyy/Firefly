import 'package:flutter/material.dart';

/// Firefly visual system: near-black surfaces, warm amber accent, soft cards.
class FireflyTheme {
  static const amber = Color(0xFFFFB347);
  static const spotify = Color(0xFF1ED760);
  static const bg = Color(0xFF0E0E10);
  static const surface = Color(0xFF17171A);
  static const surfaceHigh = Color(0xFF212126);

  static ThemeData dark() {
    final scheme = ColorScheme.fromSeed(
      seedColor: amber,
      brightness: Brightness.dark,
    ).copyWith(
      primary: amber,
      surface: surface,
      surfaceContainerHighest: surfaceHigh,
    );
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      scaffoldBackgroundColor: bg,
      cardTheme: const CardThemeData(
        color: surface,
        elevation: 0,
        margin: EdgeInsets.symmetric(vertical: 6),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(20)),
        ),
      ),
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: surface,
        indicatorColor: amber.withValues(alpha: 0.18),
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
      ),
      appBarTheme: const AppBarTheme(
        backgroundColor: bg,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(fontSize: 28, fontWeight: FontWeight.w700),
      ),
      sliderTheme: const SliderThemeData(trackHeight: 4),
      chipTheme: const ChipThemeData(
        shape: StadiumBorder(),
        side: BorderSide.none,
      ),
    );
  }
}

/// Section heading used across screens.
class SectionLabel extends StatelessWidget {
  final String text;
  final Widget? trailing;
  const SectionLabel(this.text, {super.key, this.trailing});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 18, 4, 6),
      child: Row(children: [
        Expanded(
          child: Text(
            text.toUpperCase(),
            style: TextStyle(
              fontSize: 12,
              letterSpacing: 1.2,
              fontWeight: FontWeight.w600,
              color: Colors.white.withValues(alpha: 0.55),
            ),
          ),
        ),
        if (trailing != null) trailing!,
      ]),
    );
  }
}
