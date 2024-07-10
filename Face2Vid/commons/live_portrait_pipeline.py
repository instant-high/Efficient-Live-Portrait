import cv2
import os.path as osp
from tqdm import tqdm
from Face2Vid.commons.config.inference_config import InferenceConfig
from Face2Vid.utils.cropper import Cropper
from Face2Vid.utils.camera import get_rotation_matrix
from Face2Vid.utils.video import images2video, concat_frames
from Face2Vid.utils.crop import prepare_paste_back, paste_back
from Face2Vid.utils.io import load_image_rgb, load_driving_info, resize_to_limit
from Face2Vid.utils.helper import mkdir, basename, dct2cuda, is_video
from Face2Vid.utils.rprint import rlog as log
from .live_portrait_wrapper import LivePortraitWrapper


def make_abs_path(fn):
    return osp.join(osp.dirname(osp.realpath(__file__)), fn)


class LivePortraitPipeline:

    def __init__(self, inference_cfg: InferenceConfig):
        self.live_portrait_wrapper: LivePortraitWrapper = LivePortraitWrapper(cfg=inference_cfg)
        self.cropper = Cropper(crop_cfg=inference_cfg)

    def process_crop(self, source_image, inference_cfg):
        img_rgb = load_image_rgb(source_image)
        img_rgb = resize_to_limit(img_rgb, inference_cfg.ref_max_shape, inference_cfg.ref_shape_n)
        log(f"Load source image from {source_image}")
        crop_info = self.cropper.crop_single_image(img_rgb)
        source_lmk = crop_info['lmk_crop']
        _, img_crop_256x256 = crop_info['img_crop'], crop_info['img_crop_256x256']
        return img_rgb, crop_info, source_lmk, img_crop_256x256

    def calculate_kps(self, flag_do_crop, img_rgb, img_crop_256x256):
        if flag_do_crop:
            i_s = self.live_portrait_wrapper.prepare_source(img_crop_256x256)
        else:
            i_s = self.live_portrait_wrapper.prepare_source(img_rgb)
        x_s_info = self.live_portrait_wrapper.get_kp_info(i_s)
        x_c_s = x_s_info['kp']
        r_s = get_rotation_matrix(x_s_info['pitch'], x_s_info['yaw'], x_s_info['roll'])
        f_s = self.live_portrait_wrapper.extract_feature_3d(i_s)
        x_s = self.live_portrait_wrapper.transform_keypoint(x_s_info)
        return x_s_info, x_c_s, r_s, f_s, x_s

    def calculate_lip(self, x_s, source_lmk, inference_cfg):
        if inference_cfg.flag_lip_zero:
            # let lip-open scalar to be 0 at first
            c_d_lip_before_animation = [0.]
            combined_lip_ratio_tensor_before_animation = self.live_portrait_wrapper.calc_combined_lip_ratio(
                c_d_lip_before_animation, source_lmk)
            if combined_lip_ratio_tensor_before_animation[0][0] < inference_cfg.lip_zero_threshold:
                inference_cfg.flag_lip_zero = False
            else:
                lip_delta_before_animation = self.live_portrait_wrapper.retarget_lip(x_s,
                                                                                     combined_lip_ratio_tensor_before_animation)
                return lip_delta_before_animation

    def process_source_motion(self, img_rgb, source_motion, crop_info, inference_cfg, source_lmk):
        template_lst = None
        input_eye_ratio_lst = None
        input_lip_ratio_lst = None
        if is_video(source_motion):
            log(f"Load from video file (mp4 mov avi etc...): {source_motion}")
            driving_rgb_lst = load_driving_info(source_motion)
            driving_rgb_lst_256 = [cv2.resize(_, (256, 256)) for _ in driving_rgb_lst]
            i_d_lst = self.live_portrait_wrapper.prepare_driving_videos(driving_rgb_lst_256)
            n_frames = i_d_lst.shape[0]
            if inference_cfg.flag_eye_retargeting or inference_cfg.flag_lip_retargeting:
                driving_lmk_lst = self.cropper.get_retargeting_lmk_info(driving_rgb_lst)
                input_eye_ratio_lst, input_lip_ratio_lst = self.live_portrait_wrapper.calc_retargeting_ratio(source_lmk,
                                                                                                             driving_lmk_lst)
        else:
            print("Unsupported driving types!")
            return None, None, None, None, None, None
        # if inference_cfg.flag_pasteback:
        mask_ori = prepare_paste_back(inference_cfg.mask_crop, crop_info['M_c2o'],
                                      dsize=(img_rgb.shape[1], img_rgb.shape[0]))
        i_p_paste_lst = []
        return mask_ori, driving_rgb_lst, i_d_lst, i_p_paste_lst, template_lst, n_frames, input_eye_ratio_lst, input_lip_ratio_lst

    def generate(self, n_frames, source_lmk, source_motion, crop_info, img_rgb, mask_ori, input_eye_ratio_lst,
                 input_lip_ratio_lst, i_d_lst, i_p_paste_lst, x_s, r_s, f_s, x_s_info, x_c_s,
                 lip_delta_before_animation, template_lst, inference_cfg):
        i_p_lst = []
        r_d_0, x_d_0_info = None, None
        for i in tqdm(range(n_frames), desc='Animating...', total=n_frames):
            if is_video(source_motion):
                # extract kp info by M
                i_d_i = i_d_lst[i]
                x_d_i_info = self.live_portrait_wrapper.get_kp_info(i_d_i)
                r_d_i = get_rotation_matrix(x_d_i_info['pitch'], x_d_i_info['yaw'], x_d_i_info['roll'])
            else:
                # from template
                x_d_i_info = template_lst[i]
                x_d_i_info = dct2cuda(x_d_i_info, inference_cfg.device)
                r_d_i = x_d_i_info['R_d']

            if i == 0:
                r_d_0 = r_d_i
                x_d_0_info = x_d_i_info

            if inference_cfg.flag_relative:
                r_new = (r_d_i @ r_d_0.permute(0, 2, 1)) @ r_s
                delta_new = x_s_info['exp'] + (x_d_i_info['exp'] - x_d_0_info['exp'])
                scale_new = x_s_info['scale'] * (x_d_i_info['scale'] / x_d_0_info['scale'])
                t_new = x_s_info['t'] + (x_d_i_info['t'] - x_d_0_info['t'])
            else:
                r_new = r_d_i
                delta_new = x_d_i_info['exp']
                scale_new = x_s_info['scale']
                t_new = x_d_i_info['t']

            t_new[..., 2].fill_(0)  # zero tz
            x_d_i_new = scale_new * (x_c_s @ r_new + delta_new) + t_new

            # Algorithm 1:
            if not inference_cfg.flag_stitching and not inference_cfg.flag_eye_retargeting and not inference_cfg.flag_lip_retargeting:
                # without stitching or retargeting
                if inference_cfg.flag_lip_zero:
                    x_d_i_new += lip_delta_before_animation.reshape(-1, x_s.shape[1], 3)
                else:
                    pass
            elif inference_cfg.flag_stitching and not inference_cfg.flag_eye_retargeting and not inference_cfg.flag_lip_retargeting:
                # with stitching and without retargeting
                if inference_cfg.flag_lip_zero:
                    x_d_i_new = self.live_portrait_wrapper.stitching(x_s,
                                                                     x_d_i_new) + lip_delta_before_animation.reshape(-1,
                                                                                                                     x_s.shape[
                                                                                                                         1],
                                                                                                                     3)
                else:
                    x_d_i_new = self.live_portrait_wrapper.stitching(x_s, x_d_i_new)
            else:
                eyes_delta, lip_delta = None, None
                if inference_cfg.flag_eye_retargeting:
                    c_d_eyes_i = input_eye_ratio_lst[i]
                    combined_eye_ratio_tensor = self.live_portrait_wrapper.calc_combined_eye_ratio(c_d_eyes_i,
                                                                                                   source_lmk)
                    # ∆_eyes,i = R_eyes(x_s; c_s,eyes, c_d,eyes,i)
                    eyes_delta = self.live_portrait_wrapper.retarget_eye(x_s, combined_eye_ratio_tensor)
                if inference_cfg.flag_lip_retargeting:
                    c_d_lip_i = input_lip_ratio_lst[i]
                    combined_lip_ratio_tensor = self.live_portrait_wrapper.calc_combined_lip_ratio(c_d_lip_i,
                                                                                                   source_lmk)
                    # ∆_lip,i = R_lip(x_s; c_s,lip, c_d,lip,i)
                    lip_delta = self.live_portrait_wrapper.retarget_lip(x_s, combined_lip_ratio_tensor)

                if inference_cfg.flag_relative:  # use x_s
                    x_d_i_new = x_s + \
                                (eyes_delta.reshape(-1, x_s.shape[1], 3) if eyes_delta is not None else 0) + \
                                (lip_delta.reshape(-1, x_s.shape[1], 3) if lip_delta is not None else 0)
                else:  # use x_d,i
                    x_d_i_new = x_d_i_new + \
                                (eyes_delta.reshape(-1, x_s.shape[1], 3) if eyes_delta is not None else 0) + \
                                (lip_delta.reshape(-1, x_s.shape[1], 3) if lip_delta is not None else 0)

                if inference_cfg.flag_stitching:
                    x_d_i_new = self.live_portrait_wrapper.stitching(x_s, x_d_i_new)

            out = self.live_portrait_wrapper.warp_decode(f_s, x_s, x_d_i_new)
            i_p_i = self.live_portrait_wrapper.parse_output(out['out'])[0]
            i_p_lst.append(i_p_i)

            i_p_i_to_ori_blend = paste_back(i_p_i, crop_info['M_c2o'], img_rgb, mask_ori)
            i_p_paste_lst.append(i_p_i_to_ori_blend)
        return i_p_lst

    def prepare_portrait(self, source_image_path):
        inference_cfg = self.live_portrait_wrapper.cfg  # for convenience

        # Load and preprocess source image
        img_rgb = load_image_rgb(source_image_path)
        img_rgb = resize_to_limit(img_rgb, inference_cfg.ref_max_shape, inference_cfg.ref_shape_n)
        log(f"Load source image from {source_image_path}")
        crop_info = self.cropper.crop_single_image(img_rgb)
        source_lmk = crop_info['lmk_crop']
        _, img_crop_256x256 = crop_info['img_crop'], crop_info['img_crop_256x256']

        if inference_cfg.flag_do_crop:
            i_s = self.live_portrait_wrapper.prepare_source(img_crop_256x256)
        else:
            i_s = self.live_portrait_wrapper.prepare_source(img_rgb)

        x_s_info = self.live_portrait_wrapper.get_kp_info(i_s)
        x_c_s = x_s_info['kp']
        r_s = get_rotation_matrix(x_s_info['pitch'], x_s_info['yaw'], x_s_info['roll'])
        f_s = self.live_portrait_wrapper.extract_feature_3d(i_s)
        x_s = self.live_portrait_wrapper.transform_keypoint(x_s_info)

        lip_delta_before_animation = None
        if inference_cfg.flag_lip_zero:
            c_d_lip_before_animation = [0.]
            combined_lip_ratio_tensor_before_animation = self.live_portrait_wrapper.calc_combined_lip_ratio(
                c_d_lip_before_animation, crop_info['lmk_crop'])
            if combined_lip_ratio_tensor_before_animation[0][0] < inference_cfg.lip_zero_threshold:
                inference_cfg.flag_lip_zero = False
            else:
                lip_delta_before_animation = self.live_portrait_wrapper.retarget_lip(x_s,
                                                                                     combined_lip_ratio_tensor_before_animation)
        return source_lmk, x_c_s, x_s, f_s, r_s, x_s_info, lip_delta_before_animation, crop_info, img_rgb, img_crop_256x256

    def render_offline(self, source_image, source_motion, img_rgb, img_crop_256x256, crop_info, source_lmk,
                       inference_cfg, x_s, r_s, f_s, x_s_info, x_c_s,
                       lip_delta_before_animation, output_dir='animations'):

        mask_ori, driving_rgb_lst, i_d_lst, i_p_paste_lst, \
            template_lst, n_frames, input_eye_ratio_lst, input_lip_ratio_lst = self.process_source_motion(img_rgb,
                                                                                                          source_motion,
                                                                                                          crop_info,
                                                                                                          inference_cfg,
                                                                                                          source_lmk)

        i_p_lst = self.generate(n_frames, source_lmk, source_motion, crop_info, img_rgb, mask_ori, input_eye_ratio_lst,
                                input_lip_ratio_lst, i_d_lst, i_p_paste_lst, x_s, r_s, f_s, x_s_info, x_c_s,
                                lip_delta_before_animation, template_lst, inference_cfg)
        mkdir(output_dir)
        frames_concatenated = concat_frames(i_p_lst, driving_rgb_lst, img_crop_256x256)
        wfp_concat = osp.join(output_dir,
                              f'{basename(source_image)}--{basename(source_motion)}_concat.mp4')
        images2video(frames_concatenated, wfp=wfp_concat)

        wfp = osp.join(output_dir, f'{basename(source_image)}--{basename(source_motion)}.mp4')
        images2video(i_p_paste_lst, wfp=wfp)
        return wfp, wfp_concat

    def render(self, source_image, source_motion, cam=False):
        inference_cfg = self.live_portrait_wrapper.cfg  #
        source_lmk, \
            x_c_s, x_s, f_s, r_s, x_s_info, \
            lip_delta_before_animation, crop_info, \
            img_rgb, img_crop_256x256 = self.prepare_portrait(source_image)
        # Process driving info
        if cam:
            driving_rgb = cv2.resize(source_motion, (256, 256))
            i_d_i = self.live_portrait_wrapper.prepare_driving_videos([driving_rgb])[0]

            x_d_i_info = self.live_portrait_wrapper.get_kp_info(i_d_i)
            r_d_i = get_rotation_matrix(x_d_i_info['pitch'], x_d_i_info['yaw'], x_d_i_info['roll'])

            r_new = r_d_i @ r_s
            delta_new = x_s_info['exp'] + (x_d_i_info['exp'] - x_s_info['exp'])
            scale_new = x_s_info['scale'] * (x_d_i_info['scale'] / x_s_info['scale'])
            t_new = x_s_info['t'] + (x_d_i_info['t'] - x_s_info['t'])
            t_new[..., 2].fill_(0)  # zero tz

            x_d_i_new = scale_new * (x_s @ r_new + delta_new) + t_new
            if inference_cfg.flag_lip_zero and lip_delta_before_animation is not None:
                x_d_i_new += lip_delta_before_animation.reshape(-1, x_s.shape[1], 3)

            out = self.live_portrait_wrapper.warp_decode(f_s, x_s, x_d_i_new)
            i_p_i = self.live_portrait_wrapper.parse_output(out['out'])[0]

            if inference_cfg.flag_pasteback:
                mask_ori = prepare_paste_back(inference_cfg.mask_crop, crop_info['M_c2o'],
                                              dsize=(img_rgb.shape[1], img_rgb.shape[0]))
                i_p_i_to_ori_blend = paste_back(i_p_i, crop_info['M_c2o'], img_rgb, mask_ori)
                return i_p_i_to_ori_blend, img_rgb
            else:
                return i_p_i, img_rgb
        else:
            wfp, wfp_concat = self.render_offline(
                source_image=source_image,
                source_motion=source_motion,
                img_rgb=img_rgb,
                img_crop_256x256=img_crop_256x256,
                crop_info=crop_info,
                source_lmk=source_lmk,
                inference_cfg=inference_cfg, x_s=x_s, r_s=r_s, f_s=f_s, x_s_info=x_s_info, x_c_s=x_c_s,
                lip_delta_before_animation=lip_delta_before_animation, output_dir='animations')
            return wfp, wfp_concat