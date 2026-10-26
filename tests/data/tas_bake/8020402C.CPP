#include "include_asm.h"
#include "RENDER_4/8026AC38.H"
#include "COMMON/OBJRECAT30.H"
#include "COMMON/VECTOR4F.H"
#include "COMMON/ITEMRECAT20V1.H"
#include "COMMON/SHAREDPLAYER.H"
#include "COMMON/AIPROFILE.H"
#include "COMMON/UNUSED.H"
#include "COMMON/LEVEL_ACTOR.H"
#include "COMMON/WORLD.H"
#include "COMMON/REC.H"
#include "COMMON/OBJECTLINKS180_2.H"
#include "COMMON/LISTHEADER.H"
#include "COMMON/TRIPLE.H"
#include "TYPES.H"
#include "LEVEL/8020402C.H"
#include "WEAPONS/80213ED4.H"

#if defined(VERSION_US) && !defined(NON_MATCHING)
INCLUDE_ASM("span_1000", func_8020402C_us);
#endif

#if defined(VERSION_EU_X) && !defined(NON_MATCHING)
INCLUDE_ASM("span_1000", func_80204030_eu_x);
#endif

#if defined(VERSION_EU) && !defined(NON_MATCHING)
INCLUDE_ASM("span_1000", func_80204030_eu);
#endif

#if defined(VERSION_DE) && !defined(NON_MATCHING)
INCLUDE_ASM("span_1000", func_80204030_de);
#endif

#if defined(VERSION_US_REV1) || ((defined(VERSION_US) || defined(VERSION_DE) || defined(VERSION_EU) || defined(VERSION_EU_X)) && defined(NON_MATCHING))
/* Fuzzy draft for us and de: the us-rev1 body under each version's own name. */
#if defined(VERSION_US)
#define LV_FN func_8020402C_us
#elif defined(VERSION_DE)
#define LV_FN func_80204030_de
#elif defined(VERSION_EU)
#define LV_FN func_80204030_eu
#elif defined(VERSION_EU_X)
#define LV_FN func_80204030_eu_x
#else
#define LV_FN Level_UpdateBreakableProximity
#endif
#define LV_FIELD(base, type, offset) (*(type *)((u8 *)(base) + (offset)))
extern s32 D_auto_800D70DC;
extern u8 *Weapons_FindTransientPlayer(u8 *);
extern f32 Math_Sqrtf(f32);
extern void func_8022E17C_de(void *, void *, s32);
extern void Camera_PostScreenMessage(u8 *, void *, s32);
/* 182/182 words in us-rev1 and de (local j targets aside); not exact on the ROM path only because the switch jump table (D_auto_rodata_func_8020402C_us_rev1_800C6B48) still lives in a separate rodata file; us/eu/eu-x differ in prologue size */
typedef struct LvDelta {
    f32 x, y, z;
} LvDelta;
void LV_FN(u8 *self, u8 *hit) {
    u8 *state = LV_FIELD(self, u8 *, 0x18) + 0x14;
    u16 kind = LV_FIELD(self, u16, 0xE4);
    u8 *player;
    u8 *table;
    u8 *table2;
    u8 *record;
    u8 *node;
    LvDelta d;
    f32 distance;
    u32 t1;
    switch (kind) {
    case 0x64E:
        player = Weapons_FindTransientPlayer((u8 *)&D_80145040);
        if (player != 0 && LV_FIELD(player, s32, 0x5E4) != 0 && LV_FIELD(hit, s8, 0x36) != LV_FIELD(hit, s8, 0x34)) {
            table = (u8 *)&D_80145040 + 0x1860;
            if (LV_FIELD(table, s32, 0x68) == 0 && LV_FIELD(hit, s32, 0x12C) == 0) {
                if (LV_FIELD(LV_FIELD(player, u8 *, 0x5D8), u8, 0x8F) == 1) {
                    LV_FIELD(LV_FIELD(player, u8 *, 0x5D8), u8, 0x8F) = 0;
                    LV_FIELD(LV_FIELD(player, u8 *, 0x5D8), u8, 0x90) = 1;
                    LV_FIELD(table, s32, 0x74) = 1;
                    LV_FIELD(hit, s32, 0x12C) = 1;
                    Render_PlayFlatSound(0xE0D);
                }
            }
        }
        break;
    case 0x653:
    case 0x654:
    case 0x655:
    case 0x656:
        if (LV_FIELD(hit, s8, 0x36) == LV_FIELD(hit, s8, 0x34)) {
            return;
        }
        player = Weapons_FindTransientPlayer((u8 *)&D_80145040);
        if (player == 0 || LV_FIELD(LV_FIELD(player, u8 *, 0x5D8), u8, 0x8F) == 0) {
            return;
        }
        d.x = LV_FIELD(player, f32, 8) - LV_FIELD(self, f32, 8);
        d.y = LV_FIELD(player, f32, 0xC) - LV_FIELD(self, f32, 0xC);
        d.z = LV_FIELD(player, f32, 0x10) - LV_FIELD(self, f32, 0x10);
        distance = Math_Sqrtf(d.x * d.x + d.y * d.y + d.z * d.z);
        if ((f32)(s32)distance < 122.88f) {
            LV_FIELD(LV_FIELD(player, u8 *, 0x5D8), u8, 0x8F) = 0;
            LV_FIELD(LV_FIELD(player, u8 *, 0x5D8), u8, 0x90) = 1;
            LV_FIELD(LV_FIELD(player, u8 *, 0x5D8), u16, 8) += 1;
            func_8022E17C_de(player, player, 3);
            table2 = (u8 *)&D_80145040 + 0x1860;
            LV_FIELD(table2, s32, 0x84) = 0;
            LV_FIELD(table2, s32, 0x80) = 0;
            switch (LV_FIELD(LV_FIELD(player, u8 *, 0x5D8), u8, 0x92)) {
            case 0:
                Render_PlayFlatSound(0x2E6);
                break;
            case 1:
                Render_PlayFlatSound(0x2E4);
                break;
            case 2:
                Render_PlayFlatSound(0x2E8);
                break;
            case 3:
                Render_PlayFlatSound(0x2EC);
                break;
            case 4:
                Render_PlayFlatSound(0x2EA);
                break;
            }
            for (node = *(u8 **)&D_80145060; node != 0; node = LV_FIELD(node, u8 *, 0x16E0)) {
                if (LV_FIELD(node, void *, 0x5DC) != 0) {
                    Camera_PostScreenMessage((u8 *)&D_80145088, LV_FIELD(node, void *, 0x5DC), D_auto_800D70DC);
                }
            }
        } else {
            return;
        }
        break;
    }
    if (LV_FIELD(hit, s8, 0xCB) != 0) {
        if (LV_FIELD(state, u32, 0) & 2) {
            t1 = LV_FIELD(self, u32, 0x100) & ~0x2000;
            LV_FIELD(self, u32, 0x100) = t1 & ~0x100;
        }
        if (LV_FIELD(hit, s32, 0x128) != 0) {
            Weapons_RequestActorState(self, hit, 0);
        }
        if (LV_FIELD(self, u16, 0xE4) == 0x64C) {
            Weapons_RequestActorState(self, hit, 2);
        }
    }
}
#endif

#include "COMMON/GAMEPLAY_SOUND_TABLE.H"
#include "TYPES.H"
typedef struct SoundParent { char pad0[0x5DC]; void *emitter; } SoundParent;
typedef struct SoundActor {
    u8 kind; char pad1[0xFF]; s32 flags;
    char pad104[0x1D8 - 0x104]; SoundParent *parent;
} SoundActor;
#include "COMMON/MENU_MATCH_GLOBALS.H"
extern char D_80141008;
extern void Camera_PostTargetedScreenMessage(void *, void *, s32, void *, f32);
#include "COMMON/OBJECTLINKS14.H"
/* EU/EU-X: this body is 56/57 words; reorg picks beqz (target beql) for the flags check, so those versions
 * keep their asm. */
#if (!defined(VERSION_EU) && !defined(VERSION_EU_X)) || defined(NON_MATCHING)
s32 Level_ShowTriggerMessage(struct NavigationPlatform *object, ActorMotionState *guard, struct Player *player) {
    ActorMotionDefinition *record;
    SoundParent *parent;
    void *emitter, *fallback;
    s16 index;
    s32 callback;
    record = &object->resource->motion;
    if (record->soundEnabled == 0) return 1;
    if (guard->mode != 0) return 1;
    if (((SoundActor *)player)->kind != 1) return 1;
    if ((((SoundActor *)player)->flags & 0x300000) == 0) return 1;
    index = record->soundIndex;
    parent = ((SoundActor *)player)->parent;
    if (index == 0) return 0;
    emitter = parent->emitter;
    fallback = &D_80141008;
    if (emitter != 0) fallback = emitter;
    Camera_PostTargetedScreenMessage(&D_80145088, fallback, gameplaySoundValue(index), object, gameplaySoundWeight());
    index = record->soundIndex;
    callback = gameplaySoundHook(index);
    if (callback != 0) func_8025E11C_de(callback);
    return 0;
}
#else
INCLUDE_ASM("span_1000", Level_ShowTriggerMessage);
#endif

typedef struct Owner Owner;




extern s32 Render_TestActorStateBit(void *, void *);

extern void Render_SetActorStateBit(Shared_World *, void *, s32);

extern void func_80278D78_de(void *arg0, s32 arg1, void *arg2);

struct SceneRecAt95A8;
extern void func_802A5D38_de(struct SceneRecAt95A8 *, s32);

extern void func_8028B670_de(void *a, unsigned short b, unsigned short c, int d);

/* Supplies the nonnegative blend remaining after the incoming strength is
 * scaled, then applies the actor animation using the original lookup. */
struct InputRecAt40 {
    char pad0[0x40];
    f32 strength;
};





extern void func_8024A1D0_de(Actor_func_8024A1D0_de *, void *, Lookup *);

/** Run the object's optional update hook, then copy one of two byte pairs into the record header. */







void Level_CopyTrackBytes(char *ownerRecord, char *eventRecord) {
    Owner *owner = (void *)ownerRecord;
    ObjRecAt30 *event = (void *)eventRecord;
    char *base = owner->track + 0x14;
    Hook *hook = event->unk30;

    if (hook != 0 && hook->fn != 0) {
        hook->fn(ownerRecord, eventRecord);
    }
    if (eventRecord[0x34] == 0) {
        ownerRecord[1] = base[0xE];
        ownerRecord[3] = base[0xF];
    } else {
        ownerRecord[1] = base[0x1A];
        ownerRecord[3] = base[0x1B];
    }
}

void Level_InitBreakableMotion(LevelActor *actor, ActorMotionState *motionState) {
    motionState->table = &D_800C8274;
    motionState->update = &D_002043E0;
    motionState->unk124 = 0;
    motionState->unk128 = 0;
    motionState->unk12C = 0;
    if (((BreakableDefinition *)actor->definition)->flags & 1) {
        actor->flags = actor->flags | 0x10000;
        return;
    }
    actor->flags = actor->flags & 0xFFFEFFFF;
}

/** Update actor state and clear flags for the relevant actor classes. */
void Level_ActivateBreakableState(SharedPlayer *actor, void *state) {
    s32 flags;

    if (Render_TestActorStateBit(&g_world, actor) == 1) {
        Weapons_RequestActorState(actor, state, 1);
        switch (actor->views1C.viewE4_7.kind) {
        case 0x64C:
            flags = actor->views1C.view100_9.flags & ~0x2000;
            actor->views1C.view100_9.flags = flags & ~0x100;
            break;
        case 0x64B:
        case 0x64E:
            actor->views1C.view100_9.flags &= 0xF7FFFFFF;
            break;
        }
    } else {
        D_800C8270_de = 1;
        Weapons_RequestActorState(actor, state, 0);
        D_800C8270_de = 0;
    }
}

/* The kind-10 actor initializer installs the state table containing this callback. */
s32 Level_UpdateTriggerFlag(LevelActor *actor) {
    void *resource = actor->definition;
    BreakableDefinition *definition = resource;
    s32 val;

    val = definition->triggerValue;
    if (val != 0) {
        actor->flags = actor->flags | 0x2000;
    } else {
        actor->flags = actor->flags & ~0x2000;
    }
    return val;
}

void Level_ClearBreakableState(void *arg0, void *actorMotionState) {
    ActorMotionState *state = actorMotionState;
    Render_SetActorStateBit(&g_world, arg0, 0);
    func_80278D78_de(arg0, 0x200000, arg0);
    state->unk12C = 0;
}

s32 Level_UpdateHealthFlag(LevelActor *actor) {
    s32 val;

    val = ((BreakableDefinition *)actor->definition)->health;
    if (val != 0) {
        actor->flags = actor->flags | 0x2000;
    } else {
        s32 flags = actor->flags & ~0x2000;
        flags &= ~0x100;
        actor->flags = flags;
    }
    return val;
}

void Level_SetBreakableState(void *arg0) {
    BreakableDefinition *breakableDefinition;
    s32 hitFlags;
    s32 masked;
    Shared_World *pFlag;

    breakableDefinition = (BreakableDefinition *)((LevelActor *)arg0)->definition;
    pFlag = &g_world;
    Render_SetActorStateBit(pFlag, arg0, 1);
    if (D_800C8270_de == 0) {
        func_80278D78_de(arg0, 0x80000, arg0);
    }
    func_802A5D38_de(&D_801379C0, arg0);
    if (pFlag->mode != 4) {
        hitFlags = ((LevelActor *)arg0)->flags | 0x08000000;
        ((LevelActor *)arg0)->flags = hitFlags;
        if (breakableDefinition->flags & 2) {
            masked = hitFlags & ~0x2000;
            masked = masked & ~0x100;
            ((LevelActor *)arg0)->flags = masked;
        }
        if (((BreakableDefinition *)((LevelActor *)arg0)->definition)->health == 0) {
            ((LevelActor *)arg0)->flags = ((LevelActor *)arg0)->flags & ~0x100;
        }
    }
}

void Level_SetDrawHandler(void *unused0, void *motionState) {
    Dest *state = motionState;
    state->draw = &D_00204808;
}

/** Clear two flag groups when the object's 0x1B0 field exceeds a tunable. */
void Level_ClearMotionFlags(LevelActor *object) {
    if (object->motionThreshold > D_800C1A70_de) {
        int flags = object->flags;
        flags &= ~0x2000;
        flags &= ~0x100;
        object->flags = flags;
    }
}

void Level_RequestStateOn(void *a, void *b) {
    Weapons_RequestActorState(a, b, 0x1); }

void Level_EnableTrigger(Trigger *trigger) {
    func_8028B670_de(&g_world, trigger->owner, trigger->actor, 1);
}

void Level_RequestStateOff(void *a, void *b) {
    Weapons_RequestActorState(a, b, 0x0); }

void Level_DisableTrigger(Trigger *trigger) {
    func_8028B670_de(&g_world, trigger->owner, trigger->actor, 0);
}

void Level_DrawFadedActor(Actor_func_8024A1D0_de *actor, struct InputRecAt40 *input, Lookup *lookup) {
    f32 remaining = D_800C1A78_de - input->strength * D_800C1A74_de;
    D_800D15E0 = 1;
    if (remaining < 0.0f) {
        remaining = 0.0f;
    }
    D_800D15E4[3] = remaining;
    func_8024A1D0_de(actor, input, lookup);
}

#if defined(VERSION_EU)
unsigned int func_80204808_de_padding_548C[1] = {
    0x00000000,
};
#endif

#if defined(VERSION_DE) || defined(VERSION_US)
#if defined(VERSION_DE)
unsigned int func_80204868_de_padding_5468[2] = {
    0xF041C0A8, 0xC1C218FD,
};
#endif
#if defined(VERSION_US)
char func_80204868_de_padding_5468[8] =
    "ES\004.eosR";
#endif
#endif
