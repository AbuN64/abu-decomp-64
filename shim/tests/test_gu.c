/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../n64c_gu.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"gu:%d: %s\n",__LINE__,#x); exit(1); } } while (0)
int main(void)
{
    struct { u32 before; u32 matrix[16]; u32 after; } out;
    memset(&out,0xA5,sizeof out);
    guMtxIdent(out.matrix);
    CHECK(out.before==0xA5A5A5A5u && out.after==0xA5A5A5A5u);
    double matrix[4][4];
    for (unsigned row=0;row<4;row++)
        for (unsigned column=0;column<4;column++) {
            unsigned index=row*4+column, shift=index%2 ? 0 : 16;
            s16 integral=(s16)(out.matrix[index/2]>>shift);
            u16 fraction=(u16)(out.matrix[index/2+8]>>shift);
            matrix[row][column]=(double)integral+(double)fraction/65536.0;
            CHECK(matrix[row][column]==(row==column ? 1.0 : 0.0));
        }
    const double point[4]={-2.25,3.5,8.0,1.0};
    for (unsigned row=0;row<4;row++) {
        double transformed=0;
        for (unsigned column=0;column<4;column++) transformed+=matrix[row][column]*point[column];
        CHECK(transformed==point[row]);
    }

    /* Float identity and the 16.16 packing: 1.5 -> 0x0001.8000, -0.5 -> 0xFFFF.8000, truncation toward zero. */
    f32 mf[4][4];
    guMtxIdentF(mf);
    for (unsigned row=0;row<4;row++) for (unsigned column=0;column<4;column++) CHECK(mf[row][column]==(row==column ? 1.0f : 0.0f));
    mf[0][0]=1.5f; mf[0][1]=-0.5f; mf[3][3]=-0.00001f; mf[1][2]=2.99999f;
    u32 packed[16];
    guMtxF2L(mf,packed);
    CHECK(packed[0]==0x0001FFFFu && packed[8]==0x80008000u);
    CHECK(packed[7]==0x00000000u && packed[15]==0x00000000u); /* -0.00001 truncates to zero */
    CHECK(packed[3]==0x00020000u && packed[11]==0xFFFF0000u); /* mf[1][2]=2.99999 -> 2.99998; mf[1][3]=0 */
    f32 back[4][4];
    guMtxL2F(back,packed);
    CHECK(back[0][0]==1.5f && back[0][1]==-0.5f && back[1][1]==1.0f && back[3][3]==0.0f);
    CHECK(fabsf(back[1][2]-2.99998f)<0.00002f);
    guMtxIdentF(mf); mf[0][0]=3.0e9f; mf[0][1]=-3.0e9f;
    guMtxF2L(mf,packed);
    CHECK(packed[0]==0x7FFF8000u);
    /* Look-at from (0,0,5) at the origin, y up: identity rotation, translation -5 along z, look axis +z. */
    guLookAtF(mf,0,0,5,0,0,0,0,1,0);
    for (unsigned row=0;row<3;row++) for (unsigned column=0;column<3;column++) CHECK(fabsf(mf[row][column]-(row==column ? 1.0f : 0.0f))<1e-6f);
    CHECK(fabsf(mf[3][2]+5.0f)<1e-6f && mf[3][0]==0.0f && mf[3][1]==0.0f && mf[3][3]==1.0f && mf[0][3]==0.0f);
    /* Eye on +x looking back at the origin: look axis +x, right = -z. */
    guLookAtF(mf,4,0,0,0,0,0,0,1,0);
    CHECK(fabsf(mf[2][0]+1.0f)<1e-6f && fabsf(mf[0][2]-1.0f)<1e-6f && fabsf(mf[3][2]+4.0f)<1e-6f);
    u32 look[16],ref[16];
    guLookAt(look,0,0,5,0,0,0,0,1,0);
    guLookAtF(mf,0,0,5,0,0,0,0,1,0); guMtxF2L(mf,ref);
    CHECK(memcmp(look,ref,sizeof look)==0);
    /* Perspective, 90 degrees, square, near 1 far 3: cot 1, z terms -2 and -3, norm 65536*2/4. */
    u16 norm=0;
    guPerspectiveF(mf,&norm,90.0f,1.0f,1.0f,3.0f,1.0f);
    CHECK(fabsf(mf[0][0]-1.0f)<1e-5f && fabsf(mf[1][1]-1.0f)<1e-5f && mf[2][2]==-2.0f && mf[2][3]==-1.0f && mf[3][2]==-3.0f && mf[3][3]==0.0f);
    CHECK(norm==32768);
    guPerspectiveF(mf,&norm,60.0f,2.0f,0.5f,1.0f,2.0f);
    CHECK(fabsf(mf[0][0]-2.0f*1.7320508f/2.0f)<1e-5f && mf[2][3]==-2.0f && norm==0xFFFF);
    guPerspective(look,NULL,90.0f,1.0f,1.0f,3.0f,1.0f);
    guPerspectiveF(mf,NULL,90.0f,1.0f,1.0f,3.0f,1.0f); guMtxF2L(mf,ref);
    CHECK(memcmp(look,ref,sizeof look)==0);
    /* libm wrappers: zero beyond 2^28 and for NaN, exact at the axes. */
    CHECK(n64c_sqrtf(9.0f)==3.0f && n64c_sinf(0.0f)==0.0f && n64c_cosf(0.0f)==1.0f);
    CHECK(fabsf(n64c_sinf(1.5707964f)-1.0f)<1e-6f && fabsf(n64c_cosf(3.1415927f)+1.0f)<1e-6f);
    CHECK(n64c_sinf(3.0e8f)==0.0f && n64c_cosf(-3.0e8f)==0.0f && n64c_sinf(NAN)==0.0f && n64c_cosf(INFINITY)==0.0f);
    puts("gu: packed identity preserves all basis axes and a homogeneous point");
    return 0;
}
