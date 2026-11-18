// st_probe: does renderer/third_party/shadertranslator/ShaderTranslator.h match the
// libshadertranslator.dll next to it? Compiles a highp fragment shader with
// FragmentPrecisionHigh = 1, exactly as gfxstream's ANGLE shader parser does.
//   st_probe.exe   -> prints the translator's verdict and the struct sizes we assume
#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstring>

#include "ShaderTranslator.h"

int main() {
    HMODULE m = LoadLibraryA("libshadertranslator.dll");
    if (!m) {
        printf("cannot load libshadertranslator.dll (%lu)\n", GetLastError());
        return 1;
    }
    auto init = (STInitialize_t)GetProcAddress(m, "STInitialize");
    auto gen = (STGenerateResources_t)GetProcAddress(m, "STGenerateResources");
    auto compile = (STCompileAndResolve_t)GetProcAddress(m, "STCompileAndResolve");
    if (!init || !gen || !compile) {
        printf("missing exports\n");
        return 1;
    }
    printf("sizeof(ST_BuiltInResources)=%zu sizeof(ST_ShaderCompileResult)=%zu\n",
           sizeof(ST_BuiltInResources), sizeof(ST_ShaderCompileResult));
    init();
    static ST_BuiltInResources res;
    memset(&res, 0xAB, sizeof(res));   // marks fields the DLL does not write
    gen(&res);
    printf("after STGenerateResources: MaxVertexAttribs=%d FragmentPrecisionHigh=%d "
           "MaxDrawBuffers=%d\n", res.MaxVertexAttribs, res.FragmentPrecisionHigh,
           res.MaxDrawBuffers);
    {   // OST_DUMP: the DLL's view of the struct, as ints (0xABABABAB = not written)
        const int* w = (const int*)&res;
        for (unsigned i = 0; i < sizeof(res) / 4; i++) printf("%u:%d%s", i, w[i], i % 12 == 11 ? "\n" : " ");
        printf("\nour FragmentPrecisionHigh index = %zu\n",
               (size_t)(((const char*)&res.FragmentPrecisionHigh - (const char*)&res) / 4));
    }
    res.FragmentPrecisionHigh = 1;
    static const char kFrag[] =
        "\nprecision highp float;\nvarying highp vec2 vTexCoord;\n"
        "uniform sampler2D s;\nvoid main() { gl_FragColor = texture2D(s, vTexCoord); }\n";
    ST_ShaderCompileInfo ci = {nullptr, 0x8B30 /* GL_FRAGMENT_SHADER */, ST_GLES2_SPEC,
                               ST_GLSL_COMPATIBILITY_OUTPUT, ST_OBJECT_CODE | ST_VARIABLES,
                               &res, kFrag};
    ST_ShaderCompileResult* out = nullptr;
    compile(&ci, &out);
    if (!out) {
        printf("no result\n");
        return 1;
    }
    printf("compileStatus=%d\ninfoLog: %s\ntranslated:\n%s\n", (int)out->compileStatus,
           out->infoLog ? out->infoLog : "(null)",
           out->translatedSource ? out->translatedSource : "(null)");
    return 0;
}
