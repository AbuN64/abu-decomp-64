/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The guest's virtual ranges as placeholders (os/win_space.h). */
#include "win_space.h"
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

enum { GRANULE=N64C_SPACE_GRANULE, MEM_RESERVE_=0x2000, MEM_PRIVATE_=0x20000, REGIONS=64 };
enum { HELD_NOT=0, HELD_PLACEHOLDER, HELD_VIEW, HELD_BLOCK };
/* What the game uses: a 1 MB mapping at 0x00400000 and the resource pager's pages from 0x00800000. The gap between is left to
 * the system (the loader's stack and heap lie there). */
static const struct { uintptr_t base; unsigned chunks; } zones[N64C_SPACE_ZONES]={{0x00400000u,16},{0x00800000u,32}};

static int chunk_index(uintptr_t address)
{
    unsigned first=0;
    for (unsigned z=0;z<N64C_SPACE_ZONES;z++) {
        if (address>=zones[z].base && address<zones[z].base+(uintptr_t)zones[z].chunks*GRANULE)
            return (int)(first+(address-zones[z].base)/GRANULE);
        first+=zones[z].chunks;
    }
    return -1;
}
static uintptr_t chunk_base(unsigned index)
{
    for (unsigned z=0;z<N64C_SPACE_ZONES;z++) {
        if (index<zones[z].chunks) return zones[z].base+(uintptr_t)index*GRANULE;
        index-=zones[z].chunks;
    }
    return 0;
}

void n64c_space_report(const N64cSpaceOps *ops,uintptr_t address,size_t bytes,const char *what)
{
    char line[400];
    size_t at=0;
    snprintf(line,sizeof line,"n64c: %s %#lx +%#lx, what occupies it:",what,(unsigned long)address,(unsigned long)bytes);
    ops->log(ops->context,line);
    for (unsigned n=0;n<REGIONS && at<bytes;n++) {
        N64cSpaceRegion region;
        memset(&region,0,sizeof region);
        if (!ops->query(ops->context,address+at,&region) || !region.size) {
            ops->log(ops->context,"n64c:   (no answer from the system)");
            return;
        }
        snprintf(line,sizeof line,"n64c:   base %#lx size %#lx state %#x type %#x allocation base %#lx module %s",
                 (unsigned long)region.base,(unsigned long)region.size,region.state,region.type,
                 (unsigned long)region.allocation_base,region.module[0]?region.module:"(none)");
        ops->log(ops->context,line);
        at=(size_t)(region.base+region.size-address);
    }
}

unsigned n64c_space_reserve(const N64cSpaceOps *ops)
{
    unsigned failed=0;
    for (unsigned i=0;i<N64C_SPACE_CHUNKS;i++) {
        const uintptr_t at=chunk_base(i);
        if (!ops->reserve(ops->context,at,GRANULE)) {
            failed++;
            n64c_space_report(ops,at,GRANULE,"cannot reserve the guest range");
        }
    }
    return failed;
}

unsigned n64c_space_adopt(N64cSpace *space,const N64cSpaceOps *ops)
{
    unsigned missing=0;
    memset(space,0,sizeof *space);
    space->ops=*ops;
    for (unsigned i=0;i<N64C_SPACE_CHUNKS;i++) {
        const uintptr_t at=chunk_base(i);
        N64cSpaceRegion region;
        memset(&region,0,sizeof region);
        if (ops->query(ops->context,at,&region) && region.state==MEM_RESERVE_ && region.type==MEM_PRIVATE_ &&
            region.allocation_base==at && region.size==GRANULE)
            space->chunk[i]=HELD_PLACEHOLDER;
        else {
            missing++;
            n64c_space_report(ops,at,GRANULE,"guest range not reserved at start:");
        }
    }
    return missing;
}

/* The run [address, address+chunks*GRANULE) is one placeholder: split it into chunks again. 1 when it is. */
static int split_run(N64cSpace *space,uintptr_t address,unsigned chunks)
{
    for (unsigned i=0;i+1<chunks;i++)
        if (!space->ops.release(space->ops.context,address+(uintptr_t)i*GRANULE,GRANULE)) return 0;
    return 1;
}
static void set_run(N64cSpace *space,uintptr_t address,unsigned chunks,unsigned char state)
{
    const int first=chunk_index(address);
    for (unsigned i=0;i<chunks;i++) space->chunk[first+(int)i]=state;
}

int n64c_space_view(N64cSpace *space,uintptr_t address,size_t bytes,uint32_t physical)
{
    const unsigned chunks=(unsigned)(bytes/GRANULE);
    unsigned held=0,free_held=0;
    if (address%GRANULE || bytes%GRANULE || !chunks) return 0;
    for (unsigned i=0;i<chunks;i++) {
        const int index=chunk_index(address+(uintptr_t)i*GRANULE);
        if (index>=0 && space->chunk[index]) held++;
        if (index>=0 && space->chunk[index]==HELD_PLACEHOLDER) free_held++;
    }
    if (!held) return 0;
    if (free_held!=chunks) {
        n64c_space_report(&space->ops,address,bytes,"a held guest range is not free for the view at");
        return -1;
    }
    unsigned merged=1;
    for (;merged<chunks;merged++)
        if (!space->ops.coalesce(space->ops.context,address,(size_t)(merged+1)*GRANULE)) break;
    int ok=merged==chunks && space->ops.map(space->ops.context,address,bytes,physical);
    if (!ok) {
        n64c_space_report(&space->ops,address,bytes,"cannot map the guest range");
        if (!split_run(space,address,merged)) set_run(space,address,chunks,HELD_NOT);
        return -1;
    }
    set_run(space,address,chunks,HELD_VIEW);
    return 1;
}

int n64c_space_view_end(N64cSpace *space,uintptr_t address,size_t bytes)
{
    const int first=chunk_index(address);
    const unsigned chunks=(unsigned)(bytes/GRANULE);
    if (first<0 || space->chunk[first]!=HELD_VIEW || !chunks) return 0;
    if (!space->ops.unmap(space->ops.context,address,bytes)) {
        n64c_space_report(&space->ops,address,bytes,"cannot turn the guest view back into placeholders at");
        set_run(space,address,chunks,HELD_NOT);
        return 0;
    }
    if (!split_run(space,address,chunks)) {
        n64c_space_report(&space->ops,address,bytes,"cannot split the guest range into chunks at");
        set_run(space,address,chunks,HELD_NOT);
        return 1;
    }
    set_run(space,address,chunks,HELD_PLACEHOLDER);
    return 1;
}

int n64c_space_block(N64cSpace *space,uintptr_t address)
{
    const int index=chunk_index(address);
    const uintptr_t at=address&~(uintptr_t)(GRANULE-1);
    if (index<0 || !space->chunk[index]) return 0;
    if (space->chunk[index]==HELD_BLOCK) return 1;
    if (space->chunk[index]!=HELD_PLACEHOLDER || !space->ops.alloc(space->ops.context,at,GRANULE)) {
        n64c_space_report(&space->ops,at,GRANULE,"cannot take the guest block at");
        return -1;
    }
    space->chunk[index]=HELD_BLOCK;
    return 1;
}

int n64c_space_block_end(N64cSpace *space,uintptr_t address)
{
    const int index=chunk_index(address);
    const uintptr_t at=address&~(uintptr_t)(GRANULE-1);
    if (index<0 || space->chunk[index]!=HELD_BLOCK) return 0;
    if (!space->ops.release(space->ops.context,at,GRANULE)) {
        n64c_space_report(&space->ops,at,GRANULE,"cannot turn the guest block back into a placeholder at");
        space->chunk[index]=HELD_NOT;
        return 0;
    }
    space->chunk[index]=HELD_PLACEHOLDER;
    return 1;
}
