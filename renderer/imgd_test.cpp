// Unit test of the image detection core (gfxstream GLcommon common/imgd.cpp, patch 0009).
// No GL: g++ -std=c++17 -I<gfxstream>/host/gl/glestranslator/common/include
//            imgd_test.cpp <gfxstream>/host/gl/glestranslator/common/imgd.cpp
#include "common/imgd.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>

static int g_fail = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); g_fail++; } } while (0)

static uint32_t crcRef(const uint8_t* p, size_t n) {        // HD-Player FUN_1402b52f0
    uint32_t c = 0xffffffff;
    while (n--) {
        c ^= *p++;
        for (int k = 0; k < 8; k++) c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
    }
    return ~c;
}

struct Report { uint32_t gen; std::vector<int> ids; };
static std::vector<Report> g_reports;
static void onReport(void*, uint32_t gen, const int32_t* idx, int n) {
    g_reports.push_back({gen, std::vector<int>(idx, idx + n)});
}

int main() {
    // ---- CRC32 (zlib), slicing-by-8 vs bitwise
    CHECK(imgd::crc32("123456789", 9) == 0xCBF43926u);
    std::vector<uint8_t> buf(100003);
    for (size_t i = 0; i < buf.size(); i++) buf[i] = (uint8_t)(i * 2654435761u >> 13);
    for (size_t n : {0, 1, 7, 8, 9, 63, 4096, 100003})
        CHECK(imgd::crc32(buf.data(), n) == crcRef(buf.data(), n));
    CHECK(imgd::pixelSize(0x1908, 0x1401) == 4);          // RGBA / UNSIGNED_BYTE
    CHECK(imgd::pixelSize(0x1907, 0x8363) == 2);          // RGB / 5_6_5
    CHECK(imgd::pixelSize(0x1909, 0x1401) == 1);          // LUMINANCE

    // ---- a UI quad: pos (x, y) in clip space, uv; two triangles 0 1 2 / 2 1 3
    struct V { float x, y, u, v; };
    V quad[4] = {{-0.5f, 0.5f, 0.60f, 0.10f}, {0.5f, 0.5f, 0.70f, 0.10f},
                 {-0.5f, -0.5f, 0.60f, 0.20f}, {0.5f, -0.5f, 0.70f, 0.20f}};
    imgd::Attr pos, tex;
    pos.data = tex.data = reinterpret_cast<const uint8_t*>(quad);
    pos.avail = tex.avail = sizeof(quad);
    pos.size = tex.size = 2;
    pos.type = tex.type = 0x1406;
    pos.stride = tex.stride = sizeof(V);
    tex.data += 8;
    tex.avail -= 8;
    uint16_t idx[6] = {0, 1, 2, 2, 1, 3};
    imgd::Order ord;
    ord.data = reinterpret_cast<const uint8_t*>(idx);
    ord.avail = sizeof(idx);
    ord.type = 0x1403;
    ord.count = 6;
    ord.mode = 4;
    int cur = 0, tri[3];
    CHECK(imgd::findTriangle(tex, ord, 0.62f, 0.12f, &cur, tri) && tri[0] == 0 && cur == 3);
    cur = 0;
    CHECK(imgd::findTriangle(tex, ord, 0.69f, 0.19f, &cur, tri) && tri[0] == 3);
    cur = 0;
    CHECK(!imgd::findTriangle(tex, ord, 0.5f, 0.15f, &cur, tri));        // outside
    cur = 0;
    CHECK(imgd::findTriangle(tex, ord, 0.6f, 0.1f, &cur, tri));          // a corner counts
    // the centre of triangle 2 1 3 -> (1/6, -1/6) clip -> (58.33%, 58.33%)
    cur = 0;
    imgd::findTriangle(tex, ord, 0.69f, 0.19f, &cur, tri);
    float p[4], c[4], x, y;
    CHECK(imgd::centroid(pos, ord, tri, p));
    float I[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
    imgd::unityClip(I, I, p, c);
    imgd::clipToPercent(c, &x, &y);
    CHECK(std::fabs(x - 58.333f) < 0.01f && std::fabs(y - 58.333f) < 0.01f);
    float r1[4] = {50, 50, 70, 70}, r2[4] = {0, 0, 50, 50};
    CHECK(imgd::inRect(r1, x, y) && !imgd::inRect(r2, x, y));
    // ObjectToWorld translating by +0.5 x: centre moves to 83.33% x
    float T[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0.5f, 0, 0, 1};
    imgd::unityClip(T, I, p, c);
    imgd::clipToPercent(c, &x, &y);
    CHECK(std::fabs(x - 83.333f) < 0.01f);

    // DrawArrays (no indices), as a strip 0 1 2 3
    imgd::Order arr;
    arr.count = 4;
    arr.mode = 5;
    cur = 0;
    CHECK(imgd::findTriangle(tex, arr, 0.69f, 0.19f, &cur, tri) && tri[0] == 1);
    // triangles mode over 4 vertices: only 0 1 2 is tested
    arr.mode = 4;
    cur = 0;
    CHECK(!imgd::findTriangle(tex, arr, 0.69f, 0.19f, &cur, tri));
    // reads past the data stop instead of crashing
    imgd::Order bad = ord;
    bad.count = 60;
    idx[5] = 999;
    cur = 3;
    CHECK(!imgd::findTriangle(tex, bad, 0.69f, 0.19f, &cur, tri));
    idx[5] = 3;

    // normalized unsigned short texcoords (HD-Player scales by 1/65536)
    uint16_t uv16[8] = {39322, 6554, 45875, 6554, 39322, 13107, 45875, 13107};
    imgd::Attr t16;
    t16.data = reinterpret_cast<const uint8_t*>(uv16);
    t16.avail = sizeof(uv16);
    t16.size = 2;
    t16.type = 0x1403;
    t16.normalized = true;
    cur = 0;
    CHECK(imgd::findTriangle(t16, ord, 0.62f, 0.12f, &cur, tri));
    t16.normalized = false;                                   // raw values: outside [0,1]
    cur = 0;
    CHECK(!imgd::findTriangle(t16, ord, 0.62f, 0.12f, &cur, tri));

    // degenerate triangles are skipped
    V flat[3] = {{0, 0, 0.5f, 0.5f}, {0, 0, 0.5f, 0.6f}, {0, 0, 0.5f, 0.7f}};
    imgd::Attr tf = tex;
    tf.data = reinterpret_cast<const uint8_t*>(flat) + 8;
    tf.avail = sizeof(flat) - 8;
    imgd::Order o3;
    o3.count = 3;
    cur = 0;
    CHECK(!imgd::findTriangle(tf, o3, 0.5f, 0.6f, &cur, tri));

    // ---- config + per-frame reports
    imgd::setReport(onReport, nullptr);
    CHECK(!imgd::armed());
    imgd::begin(7, imgd::EMU_UNITY, "_AlphaTex", "_NormalTex");
    imgd::Image a, b;
    a.crc = 0x990CCA42;
    b.crc = 0xCDA59516;
    imgd::add(a);
    imgd::add(b);
    imgd::commit();
    CHECK(imgd::armed());
    CHECK(imgd::current()->images.size() == 2 && imgd::current()->byCrc.count(0x990CCA42) == 1);
    imgd::frameEnd();                                          // nothing ever matched: quiet
    CHECK(g_reports.empty());
    imgd::markFound(1);
    CHECK(imgd::isFound(1) && !imgd::isFound(0));
    imgd::frameEnd();
    CHECK(g_reports.size() == 1 && g_reports[0].gen == 7 && g_reports[0].ids == std::vector<int>{1});
    CHECK(!imgd::isFound(1));                                  // cleared for the next frame
    imgd::markFound(1);
    imgd::frameEnd();                                          // same tags: no report
    CHECK(g_reports.size() == 1);
    imgd::frameEnd();                                          // nothing this frame: empty report
    CHECK(g_reports.size() == 2 && g_reports[1].ids.empty());
    std::thread other([] { imgd::frameEnd(); });               // a thread that never matched
    other.join();
    CHECK(g_reports.size() == 2);
    imgd::begin(8, 0, "", "");
    imgd::commit();                                            // empty list: off
    CHECK(!imgd::armed());
    imgd::markFound(0);
    imgd::frameEnd();
    CHECK(g_reports.size() == 2);

    printf(g_fail ? "%d FAILED\n" : "imgd core: all checks passed\n", g_fail);
    return g_fail != 0;
}
